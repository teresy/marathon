package mesosphere.marathon
package core.election

import akka.{Done, NotUsed}
import akka.actor.{ActorSystem, Cancellable}
import akka.event.EventStream
import akka.stream.ClosedShape
import akka.stream.scaladsl.{Broadcast, Flow, GraphDSL, Keep, RunnableGraph, Sink, Source}
import akka.stream.OverflowStrategy
import akka.stream.ActorMaterializer
import com.typesafe.scalalogging.StrictLogging
import java.util.concurrent.atomic.AtomicBoolean

import mesosphere.marathon.core.async.ExecutionContexts
import mesosphere.marathon.core.base.CrashStrategy
import mesosphere.marathon.metrics.Metrics
import mesosphere.marathon.metrics.current.UnitOfMeasurement
import mesosphere.marathon.stream.{EnrichedFlow, Subject}

import scala.concurrent.{ExecutionContext, Future}
import scala.util.{Failure, Success}

trait ElectionServiceLeaderInfo {
  /**
    * isLeader checks whether this instance is the leader, and is initialized
    *
    * @return true if this instance is the leader, and is initialized
    */
  def isLeader: Boolean

  /**
    * localHostPort return a host:port pair of this running instance that is used for discovery.
    *
    * @return host:port of this instance.
    */
  def localHostPort: String

  /**
    * leaderHostPort return a host:port pair of the leader, if it is elected.
    *
    * @return Some(host:port) of the leader, or None if no leader exists or is known
    */
  def leaderHostPort: Option[String]
}

/**
  * ElectionService is implemented by leadership election mechanisms.
  *
  * This trait is used in conjunction with [[ElectionCandidate]]. From their point of view,
  * a leader election works as follow:
  *
  * -> ElectionService.offerLeadership(candidate)     |      - A leader election is triggered.
  *                                                          — Once `candidate` is elected as a leader,
  *                                                            its `startLeadership` is called.
  *
  * Please note that upon a call to [[ElectionService.abdicateLeadership]], or
  * any error in any of method of [[ElectionService]], or a leadership loss,
  * [[ElectionCandidate.stopLeadership]] is called if [[ElectionCandidate.startLeadership]]
  * has been called before, and JVM gets shutdown.
  *
  * It effectively means that a particular instance of Marathon can be elected at most once during its lifetime.
  */
trait ElectionService extends ElectionServiceLeaderInfo {

  /**
    * offerLeadership is called to candidate for leadership. It must be called by candidate only once.
    *
    * @param candidate is called back once elected or defeated
    */
  def offerLeadership(candidate: ElectionCandidate): Unit

  /**
    * abdicateLeadership is called to resign from leadership. By the time this method returns,
    * it can be safely assumed the leadership has been abdicated. This method can be called even
    * if [[offerLeadership]] wasn't called prior to that, and it will result in Marathon stop and JVM shutdown.
    */
  def abdicateLeadership(): Unit

  /**
    * Provides LeadershipTransitions via a materializable Akka Stream
    *
    * The first element will be the current state. Upon becoming a leader, [[LeadershipTransition.ElectedAsLeaderAndReady]] is
    * published. Upon leadership loss, [[LeadershipTransition.Standby]] is sent.
    */
  def leadershipTransitionEvents: Source[LeadershipTransition, Cancellable]
}

/**
  * ElectionCandidate is implemented by a leadership election candidate. There is only one
  * ElectionCandidate per ElectionService.
  */
trait ElectionCandidate {
  /**
    * stopLeadership is called when the candidate was leader, but was defeated. It is guaranteed
    * that calls to stopLeadership and startLeadership alternate and are synchronized.
    */
  def stopLeadership(): Unit

  /**
    * startLeadership is called when the candidate has become leader. It is guaranteed
    * that calls to stopLeadership and startLeadership alternate and are synchronized.
    */
  def startLeadership(): Unit
}

/**
  * ElectionService implementation
  *
  * leaderEventsSource is a materializable Akka stream that has the following expectations:
  *
  * - If leadership status is in doubt, it should crash
  * - If the leadership connection is requested to close, it should terminate (gracefully).
  *
  * @param eventStream The event bus over which to publish the leadership messages
  * @param hostPort The host and port of this Marathon instance.
  * @param leaderEventsSource The election backend, with properties as described above
  * @param crashStrategy Called if leadership status because uncertain, or leadership abdicates.
  * @param electionEC the execution context in which to run the synchronous, blocking leader initialization logic
  */
class ElectionServiceImpl(
    metrics: Metrics,
    eventStream: EventStream,
    hostPort: String,
    leaderEventsSource: Source[LeadershipState, Cancellable],
    crashStrategy: CrashStrategy,
    electionEC: ExecutionContext
)(implicit system: ActorSystem) extends ElectionService with StrictLogging {

  @volatile private[this] var lastState: LeadershipState = LeadershipState.Standby(None)
  @volatile private[this] var _leaderAndReady: Boolean = false
  implicit private lazy val materializer = ActorMaterializer()
  var leaderSubscription: Option[Cancellable] = None
  private val offerLeadershipCalled = new AtomicBoolean(false)

  override def isLeader: Boolean =
    _leaderAndReady

  override def localHostPort: String = hostPort

  override def leaderHostPort: Option[String] = lastState match {
    case LeadershipState.ElectedAsLeader =>
      Some(hostPort)
    case LeadershipState.Standby(currentLeader) =>
      currentLeader
  }

  /**
    * Releases leadership
    *
    * Has no effect if called before offerLeadership
    */
  override def abdicateLeadership(): Unit = {
    val e = new Exception("abdicateLeadership")
    logger.error("abdicateLeadership was called", e)
    leaderSubscription.foreach(_.cancel())
  }

  /**
    * Monitor LeadershipState events, and update the state in this class so that the last received state can be easily
    * queried (IE isLeader, leaderHostPort, etc.)
    */
  private val localEventListenerSink = Sink.foreach[LeadershipState] { event =>
    lastState = event
  }

  /**
    * Monitor leader transition events. Specifically, crashes if we lose leadership.
    */
  private val localTransitionSink = Sink.foreach[LeadershipTransition] { e =>
    eventStream.publish(e)
    e match {
      case LeadershipTransition.ElectedAsLeaderAndReady =>
        _leaderAndReady = true
      case LeadershipTransition.Standby =>
        _leaderAndReady = false
        logger.error("Lost leadership; crashing")
        crashStrategy.crash(CrashStrategy.LeadershipLoss)
    }
  }

  /**
    * Construct the stream topology and run it.
    *
    * Topology looks like this:
    *
    * +----------------------------+    +-----------+    +----------------------------+
    * |    leaderEventsSource      | -> | broadcast | -> |   localEventListenerSink   |
    * | (IE CuratorElectionStream) |    +-----------+    | (keep track of last state) |
    * |                            |          |          +----------------------------+
    * |     [LeadershipState]      |          |
    * +----------------------------+          |
    *                                         V
    *                             +--------------------------------------+
    *                             |      leadershipTransitionFlow        |
    *                             | (Emits LeadershipTransition when we  |
    *                             | lost or gain leadership, but calling |
    *                             | marathon initialization logic before |
    *                             | emitting ObtainedLeadership          |
    *                             |                                      |
    *                             |      [LeadershipTransition]          |
    *                             +--------------------------------------+
    *                                              |
    *                                              V
    *          +------------------------+    +-----------+    +------------------------+
    *          |   localTransitionSink  | <- | broadcast | -> |    metricsSink         |
    *          | Monitors for lost      |    +-----------+    | Watches transitions,   |
    *          | leadership; crashes if |                     | records how long we've |
    *          | that happens.          |                     | had leadership         |
    *          +------------------------+                     +------------------------+
    *
    * If any component throws an exception, or finishes, then the _whole stream_ shuts down.
    *
    * When the stream ends (exception or not), we report accordingly and crash.
    */
  private def initializeStream(leadershipTransitionsFlow: Flow[LeadershipState, LeadershipTransition, NotUsed]) = {
    val graph = GraphDSL.create(leaderEventsSource, localEventListenerSink, localTransitionSink, metricsSink){
      (leaderStream, localEventListenerSinkR, localTransitionSinkR, metricsSinkR) =>
        import system.dispatcher
        val aggregateResult = for {
          _ <- localEventListenerSinkR
          _ <- localTransitionSinkR
          _ <- metricsSinkR
        } yield Done
        (leaderStream, aggregateResult)
    } { implicit b =>
      { (leaderEventsSource, localEventListenerSink, localTransitionSink, metricsSink) =>
        import GraphDSL.Implicits._
        // We defensively specify eagerCancel as true; if any of the components in the stream close or fail, then
        // we'll help to make it obvious by closing the entire graph (and, by consequence, crashing).
        // Akka will log all stream failures, by default.
        val stateBroadcast = b.add(Broadcast[LeadershipState](2, eagerCancel = true))
        val transitionBroadcast = b.add(Broadcast[LeadershipTransition](3, eagerCancel = true))
        val leadershipTransitionEventsInput = b.add(Sink.fromSubscriber(leadershipTransitionsEventsSubscriber))
        leaderEventsSource ~> stateBroadcast.in
        stateBroadcast ~> localEventListenerSink
        stateBroadcast ~> leadershipTransitionsFlow ~> transitionBroadcast.in

        transitionBroadcast ~> metricsSink
        transitionBroadcast ~> localTransitionSink
        transitionBroadcast ~> leadershipTransitionEventsInput
        ClosedShape
      }
    }

    val (leaderStream, leaderStreamDone) = RunnableGraph.fromGraph(graph).run

    // When the leadership stream terminates, for any reason, we suicide
    leaderStreamDone.onComplete {
      case Failure(ex) =>
        logger.info("Leadership ended with failure; exiting", ex)
        crashStrategy.crash(CrashStrategy.LeadershipEndedFaulty)
      case Success(_) =>
        logger.info("Leadership ended gracefully; exiting")
        crashStrategy.crash(CrashStrategy.LeadershipEndedGracefully)
    }(ExecutionContexts.callerThread)

    leaderStream
  }

  /**
    * offerLeadership is called to candidate for leadership. It must be called by candidate only once.
    *
    * @param candidate is called back once elected or defeated
    */
  def offerLeadership(candidate: ElectionCandidate): Unit = {
    if (!offerLeadershipCalled.compareAndSet(false, true))
      throw new IllegalStateException("You cannot call offerLeadership twice")

    /**
      * Deduped event stream with current leader removed. Specified this way to maintain compatibility with the rest of
      * the code base.
      *
      * Does not emit an event if the first events are Standby.
      */
    val leadershipTransitionsFlow =
      Flow[LeadershipState]
        .map {
          case LeadershipState.ElectedAsLeader => true
          case _: LeadershipState.Standby => false
        }
        .via(EnrichedFlow.dedup(initialFilterElement = false)) // Until we become leader, we emit nothing
        .mapAsync(1) { becameLeader =>
          if (becameLeader)
            Future {
              candidate.startLeadership()
              LeadershipTransition.ElectedAsLeaderAndReady
            }(electionEC)
          else
            Future {
              candidate.stopLeadership()
              LeadershipTransition.Standby
            }(electionEC)
        }

    leaderSubscription = Some(initializeStream(leadershipTransitionsFlow))
  }

  private[this] val (leadershipTransitionsEventsSubscriber, _leadershipTransitionEvents) =
    Source.asSubscriber[LeadershipTransition]
      .prepend(Source.single(LeadershipTransition.Standby))
      // Subject keeps track of the last item received, and published updates going forward
      // If they get behind, we can safely drop old updates as the last state matters most here.
      .toMat(Subject(32, OverflowStrategy.dropHead))(Keep.both)
      .run

  val leadershipTransitionEvents: Source[LeadershipTransition, Cancellable] = _leadershipTransitionEvents

  private[this] val leaderDurationMetric = "leadership.duration"

  private[this] val metricsSink = Sink.foreach[LeadershipTransition] {
    case LeadershipTransition.ElectedAsLeaderAndReady =>
      val startedAt = System.currentTimeMillis()
      metrics.closureGauge(
        leaderDurationMetric,
        () => (System.currentTimeMillis() - startedAt).toDouble / 1000.0, unit = UnitOfMeasurement.Time)

    case LeadershipTransition.Standby =>
  }
}

/**
  * Events produced by Curator election stream; describes transitions from one leader to the next while not leader
  *
  */
private[election] sealed trait LeadershipState
private[election] object LeadershipState {
  /**
    * Indicates that our election backend has said we are the leader; emitted _before_ Marathon initialization
    * routine.
    */
  case object ElectedAsLeader extends LeadershipState

  /**
    * Indicates that we are not the leader.
    *
    * @param currentLeader The id of the current leader, if any is known.
    */
  case class Standby(currentLeader: Option[String]) extends LeadershipState
}

/** Local leadership transition events */
sealed trait LeadershipTransition
object LeadershipTransition {
  /**
    * Emitted when we are elected as leader, _after_ Marathon is initialized
    */
  case object ElectedAsLeaderAndReady extends LeadershipTransition

  /**
    * Indicates that we previously had leadership, but now we don't.
    */
  case object Standby extends LeadershipTransition
}
