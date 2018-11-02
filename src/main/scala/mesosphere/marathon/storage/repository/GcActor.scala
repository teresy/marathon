package mesosphere.marathon
package storage.repository

import akka.stream.scaladsl.Sink
import java.time.{Duration, Instant, OffsetDateTime}

import akka.{Done, NotUsed}
import akka.actor.{ActorRef, ActorRefFactory, FSM, LoggingFSM, Props}
import akka.pattern._
import akka.stream.Materializer
import akka.stream.scaladsl.Source
import com.typesafe.scalalogging.StrictLogging
import mesosphere.marathon.core.deployment.DeploymentPlan
import mesosphere.marathon.metrics.Metrics
import mesosphere.marathon.state.{PathId, RootGroup}
import mesosphere.marathon.storage.repository.GcActor.{CompactDone, _}
import mesosphere.marathon.stream.EnrichedSink

import scala.async.Async.{async, await}
import scala.collection.{SortedSet, mutable}
import scala.concurrent.{ExecutionContext, Future, Promise}
import scala.util.control.NonFatal
import scala.concurrent.duration._

/**
  * Actor which manages Garbage Collection. Garbage Collection may be triggered by anything
  * but we currently trigger it from DeploymentRepository.delete as DeploymentPlans "are at the top" of the
  * dependency graph: deploymentPlan -> root*2 -> apps.
  *
  * The actor is very conservative about deleting and will prefer extra objects (that will likely eventually
  * be deleted) than having objects that refer to objects that no longer exist.
  *
  * The actor has three phases:
  * - Resting (nothing happening at all and we ignore all GC requests)
  * - ReadyForGc (nothing happening at all but we're ready to start GC an any moment)
  * - Scanning
  * - Compacting
  *
  * Resting Phase
  * In order to save CPU, we don't run GC for every request but rather sleep for a configured amount of time
  * (cleaning interval). During this phase all GC requests are ignored.
  *
  * Scan Phase
  * - if the total number of root versions is < maxVersions, do nothing
  * - if the total number of root versions is > maxVersions and every root is referred to by a deployment plan,
  *   do nothing.
  * - Otherwise, the oldest unused roots are picked for deletion (to get back under the cap) and we will then
  *   scan look at the [[StoredGroup]]s (we don't need to retrieve/resolve them) and find all of the app
  *   versions they are using.
  *   - We then compare this against the set of app ids that exist and find any app ids that
  *     no root refers to.
  *   - We also scan through the apps that are in use and find only the apps that have more than the cap.
  *     We take these apps and remove any versions which are not in use by any root.
  * - While the scan phase is in progress, all requests to store a Plan/Group/App will be tracked so
  *   that we can remove them from the set of deletions.
  * - When the scan is complete, we will take the set of deletions and enter into the Compacting phase.
  * - If scan fails for any reason, either return to Resting (if no further GCs were requested)
  *   or back into Scanning (if further GCs were requested). The additional GCs are coalesced into a single
  *   GC run.
  *
  * Compaction Phase:
  * - Go actually delete the objects from the database in the background.
  * - While deleting, check any store requests to see if they _could_ conflict with the in progress deletions.
  *   If and only if there is a conflict, 'block' the store (via a promise/future) until the deletion completes.
  *   If there isn't a conflict, let it save anyway.
  * - When the deletion completes, inform any attempts to store a potential conflict that it may now proceed,
  *   then transition back to resting or scanning depending on whether or not one or more additional GC Requests
  *   were sent to the actor.
  * - If compact fails for any reason, transition back to resting or scanning depending on whether or not one or
  *   more additional GC Requests were sent to the actor.
  */
private[storage] class GcActor[K, C, S](
    val metrics: Metrics,
    val deploymentRepository: DeploymentRepositoryImpl[K, C, S],
    val groupRepository: StoredGroupRepositoryImpl[K, C, S],
    val appRepository: AppRepositoryImpl[K, C, S],
    val podRepository: PodRepositoryImpl[K, C, S],
    val maxVersions: Int,
    val scanBatchSize: Int = 32,
    val cleaningInteveral: FiniteDuration)(implicit val mat: Materializer, val ctx: ExecutionContext)
  extends FSM[State, Data] with LoggingFSM[State, Data] with ScanBehavior[K, C, S] with CompactBehavior[K, C, S] {

  private val totalGcsMetric = metrics.counter("persistence.gc.runs")

  private var lastScanStart = Instant.now()
  private val scanTimeMetric = metrics.timer("persistence.gc.scan.duration")

  private var lastCompactStart = Instant.now()
  private val compactTimeMetric = metrics.timer("persistence.gc.compaction.duration")

  if (cleaningInteveral <= 0.millis) {
    startWith(ReadyForGc, EmptyData)
  } else {
    startWith(Resting, EmptyData)
  }

  when(Resting) {
    case Event(WakeUp, _) =>
      goto(ReadyForGc) using EmptyData
    case Event(StoreEntity(promise), _) =>
      promise.success(Done)
      stay
    case Event(_: Message, _) =>
      stay
    // ignore
  }

  when(ReadyForGc) {
    case Event(RunGC, _) =>
      scan().pipeTo(self)
      goto(Scanning) using UpdatedEntities()
    case Event(StoreEntity(promise), _) =>
      promise.success(Done)
      stay
    case Event(_: Message, _) =>
      stay
    // ignore
  }

  onTransition {
    case _ -> Resting =>
      setTimer(ScanIntervalTimerName, WakeUp, cleaningInteveral, repeat = false)
    case ReadyForGc -> Scanning =>
      lastScanStart = Instant.now()
    case Scanning -> Compacting =>
      lastCompactStart = Instant.now()
      val scanDuration = Duration.between(lastScanStart, lastCompactStart)
      log.info(s"Completed scan phase in $scanDuration")
      scanTimeMetric.update(scanDuration.toNanos)
    case Scanning -> ReadyForGc =>
      val scanDuration = Duration.between(lastScanStart, Instant.now)
      log.info(s"Completed empty scan in $scanDuration")
      scanTimeMetric.update(scanDuration.toNanos)
    case Compacting -> ReadyForGc | Compacting -> Resting =>
      val compactDuration = Duration.between(lastCompactStart, Instant.now)
      log.info(s"Completed compaction in $compactDuration")
      compactTimeMetric.update(compactDuration.toNanos)
      totalGcsMetric.increment()
    case Compacting -> Scanning =>
      lastScanStart = Instant.now()
      val compactDuration = Duration.between(lastCompactStart, Instant.now)
      log.info(s"Completed compaction in $compactDuration")
      compactTimeMetric.update(compactDuration.toNanos)
      totalGcsMetric.increment()
  }

  initialize()
}

private[storage] trait ScanBehavior[K, C, S] extends StrictLogging { this: FSM[State, Data] with CompactBehavior[K, C, S] =>
  implicit val mat: Materializer
  implicit val ctx: ExecutionContext
  val maxVersions: Int
  val appRepository: AppRepositoryImpl[K, C, S]
  val podRepository: PodRepositoryImpl[K, C, S]
  val groupRepository: StoredGroupRepositoryImpl[K, C, S]
  val deploymentRepository: DeploymentRepositoryImpl[K, C, S]
  val self: ActorRef
  def scanBatchSize: Int

  when(Scanning) {
    case Event(RunGC, updates: UpdatedEntities) =>
      stay using updates.copy(gcRequested = true)
    case Event(done: ScanDone, updates: UpdatedEntities) =>
      if (done.isEmpty) {
        if (updates.gcRequested) {
          scan().pipeTo(self)
          goto(Scanning) using UpdatedEntities()
        } else {
          if (cleaningInteveral <= 0.millis) {
            goto(ReadyForGc) using EmptyData
          } else {
            goto(Resting) using EmptyData
          }
        }
      } else {
        val deletes =
          computeActualDeletions(updates.appsStored, updates.appVersionsStored,
            updates.podsStored, updates.podVersionsStored, updates.rootsStored, done)
        compact(
          deletes.appsDeleting,
          deletes.appVersionsDeleting,
          deletes.podsDeleting,
          deletes.podVersionsDeleting,
          deletes.rootsDeleting).pipeTo(self)
        goto(Compacting) using deletes.copy(gcRequested = updates.gcRequested)
      }
    case Event(StoreApp(appId, Some(version), promise), updates: UpdatedEntities) =>
      promise.success(Done)
      val appVersions = updates.appVersionsStored + (appId -> (updates.appVersionsStored(appId) + version))
      stay using updates.copy(appVersionsStored = appVersions)
    case Event(StoreApp(appId, _, promise), updates: UpdatedEntities) =>
      promise.success(Done)
      stay using updates.copy(appsStored = updates.appsStored + appId)
    case Event(StorePod(podId, Some(version), promise), updates: UpdatedEntities) =>
      promise.success(Done)
      val podVersions = updates.podVersionsStored + (podId -> (updates.podVersionsStored(podId) + version))
      stay using updates.copy(podVersionsStored = podVersions)
    case Event(StorePod(podId, _, promise), updates: UpdatedEntities) =>
      promise.success(Done)
      stay using updates.copy(podsStored = updates.podsStored + podId)
    case Event(StoreRoot(root, promise), updates: UpdatedEntities) =>
      promise.success(Done)
      val appVersions = addAppVersions(root.transitiveAppIds, updates.appVersionsStored)
      stay using updates.copy(rootsStored = updates.rootsStored + root.version, appVersionsStored = appVersions)
    case Event(StorePlan(plan, promise), updates: UpdatedEntities) =>
      promise.success(Done)
      val originalUpdates =
        addAppVersions(
          plan.original.transitiveApps.map(app => app.id -> app.version.toOffsetDateTime)(collection.breakOut),
          updates.appVersionsStored)
      val allUpdates =
        addAppVersions(plan.target.transitiveApps.map(app => app.id -> app.version.toOffsetDateTime)(collection.breakOut), originalUpdates)
      val newRootsStored = updates.rootsStored ++
        Set(plan.original.version.toOffsetDateTime, plan.target.version.toOffsetDateTime)
      stay using updates.copy(appVersionsStored = allUpdates, rootsStored = newRootsStored)
    case Event(_: Message, _) =>
      stay
  }

  def computeActualDeletions(
    appsStored: Set[PathId],
    appVersionsStored: Map[PathId, Set[OffsetDateTime]],
    podsStored: Set[PathId],
    podVersionsStored: Map[PathId, Set[OffsetDateTime]],
    rootsStored: Set[OffsetDateTime],
    scanDone: ScanDone): BlockedEntities = {
    val ScanDone(appsToDelete, appVersionsToDelete, podsToDelete, podVersionsToDelete, rootVersionsToDelete) = scanDone
    val appsToActuallyDelete = appsToDelete.diff(appsStored.union(appVersionsStored.keySet))
    val appVersionsToActuallyDelete = appVersionsToDelete.map {
      case (id, versions) =>
        appVersionsStored.get(id).fold(id -> versions) { versionsStored =>
          id -> versions.diff(versionsStored)
        }
    }
    val podsToActuallyDelete = podsToDelete.diff(podsStored.union(podVersionsStored.keySet))
    val podVersionsToActualllyDelete = podVersionsToDelete.map {
      case (id, versions) =>
        podVersionsStored.get(id).fold(id -> versions) { versionsStored =>
          id -> versions.diff(versionsStored)
        }
    }
    val rootsToActuallyDelete = rootVersionsToDelete.diff(rootsStored)
    BlockedEntities(appsToActuallyDelete, appVersionsToActuallyDelete,
      podsToActuallyDelete, podVersionsToActualllyDelete, rootsToActuallyDelete)
  }

  def addAppVersions(
    apps: Map[PathId, OffsetDateTime],
    appVersionsStored: Map[PathId, Set[OffsetDateTime]]): Map[PathId, Set[OffsetDateTime]] = {
    apps.foldLeft(appVersionsStored) {
      case (appVersions, (pathId, version)) =>
        appVersions + (pathId -> (appVersions(pathId) + version))
    }
  }

  def scan(): Future[ScanDone] = {
    async { // linter:ignore UnnecessaryElseBranch
      val rootVersions = await(groupRepository.rootVersions().runWith(EnrichedSink.sortedSet))
      if (rootVersions.size <= maxVersions) {
        ScanDone(Set.empty, Map.empty, Set.empty)
      } else {
        val currentRootFuture = groupRepository.root()
        val storedPlansFuture = deploymentRepository.lazyAll().runWith(EnrichedSink.list)
        val currentRoot = await(currentRootFuture)
        val storedPlans = await(storedPlansFuture)

        val currentlyInDeployment: SortedSet[OffsetDateTime] = storedPlans.flatMap { plan =>
          Seq(plan.originalVersion, plan.targetVersion)
        }(collection.breakOut)

        val deletionCandidates = rootVersions.diff(currentlyInDeployment + currentRoot.version.toOffsetDateTime)

        if (deletionCandidates.isEmpty) {
          ScanDone(Set.empty, Map.empty, Set.empty)
        } else {
          val rootsToDelete = deletionCandidates.take(rootVersions.size - maxVersions)
          if (rootsToDelete.isEmpty) {
            ScanDone(Set.empty, Map.empty, Set.empty)
          } else {
            await(scanUnusedAppsAndPods(rootsToDelete, storedPlans, currentRoot))
          }
        }
      }
    }.recover {
      case NonFatal(e) =>
        logger.error(s"Error while scanning for unused roots ${Option(e.getMessage).getOrElse("")}: ", e)
        ScanDone()
    }
  }

  private def scanUnusedAppsAndPods(
    rootsToDelete: Set[OffsetDateTime],
    storedPlans: Seq[StoredPlan],
    currentRoot: RootGroup): Future[ScanDone] = {

    def appsInUse(roots: Seq[StoredGroup]): Map[PathId, Set[OffsetDateTime]] = {
      val appVersionsInUse = new mutable.HashMap[PathId, mutable.Set[OffsetDateTime]] with mutable.MultiMap[PathId, OffsetDateTime]
      currentRoot.transitiveAppsIterator().foreach { app =>
        appVersionsInUse.addBinding(app.id, app.version.toOffsetDateTime)
      }
      roots.foreach { root =>
        root.transitiveAppIds.foreach {
          case (id, version) =>
            appVersionsInUse.addBinding(id, version)
        }
      }
      appVersionsInUse.map { case (id, apps) => id -> apps.to[Set] }(collection.breakOut)
    }

    def podsInUse(roots: Seq[StoredGroup]): Map[PathId, Set[OffsetDateTime]] = {
      val podVersionsInUse = new mutable.HashMap[PathId, mutable.Set[OffsetDateTime]] with mutable.MultiMap[PathId, OffsetDateTime]
      currentRoot.transitivePodsIterator().foreach { pod =>
        podVersionsInUse.addBinding(pod.id, pod.version.toOffsetDateTime)
      }
      roots.foreach { root =>
        root.transitivePodIds.foreach {
          case (id, version) =>
            podVersionsInUse.addBinding(id, version)
        }
      }
      podVersionsInUse.map { case (id, pods) => id -> pods.to[Set] }(collection.breakOut)
    }

    def rootsInUse(): Source[StoredGroup, NotUsed] = {
      Source(storedPlans)
        .mapConcat(plan => Seq(plan.originalVersion, plan.targetVersion))
        .mapAsync(1)(groupRepository.lazyRootVersion)
        .mapConcat(_.toList)
    }

    def appsExceedingMaxVersions(usedApps: Set[PathId]): Future[Map[PathId, Set[OffsetDateTime]]] = {
      Source(usedApps)
        .mapAsync(1)(id => appRepository.versions(id).runWith(EnrichedSink.sortedSet).map(id -> _))
        .filter(_._2.size > maxVersions)
        .runWith(EnrichedSink.map)
    }

    def podsExceedingMaxVersions(usedPods: Set[PathId]): Future[Map[PathId, Set[OffsetDateTime]]] = {
      Source(usedPods)
        .mapAsync(1)(id => podRepository.versions(id).runWith(EnrichedSink.sortedSet).map(id -> _))
        .filter(_._2.size > maxVersions)
        .runWith(EnrichedSink.map)
    }

    val allAppIdsFuture = appRepository.ids().runWith(EnrichedSink.set)
    val allPodIdsFuture = podRepository.ids().runWith(EnrichedSink.set)

    rootsInUse()
      .grouped(scanBatchSize)
      .mapAsync(1) { inUseRoots => //inUseRoots has size of scanBatchSize
        async { // linter:ignore UnnecessaryElseBranch
          val allAppIds = await(allAppIdsFuture)
          val allPodIds = await(allPodIdsFuture)
          val usedApps = appsInUse(inUseRoots)
          val usedPods = podsInUse(inUseRoots)
          val appsWithTooManyVersions = await(appsExceedingMaxVersions(usedApps.keySet))
          val podsWithTooManyVersions = await(podsExceedingMaxVersions(usedPods.keySet))

          val appVersionsToDelete = appsWithTooManyVersions.map {
            case (id, versions) =>
              val candidateVersions = versions.diff(usedApps.getOrElse(id, SortedSet.empty))
              id -> candidateVersions.take(versions.size - maxVersions)
          }

          val podVersionsToDelete = podsWithTooManyVersions.map {
            case (id, versions) =>
              val candidateVersions = versions.diff(usedPods.getOrElse(id, SortedSet.empty))
              id -> candidateVersions.take(versions.size - maxVersions)
          }

          val appsToCompletelyDelete = allAppIds.diff(usedApps.keySet)
          val podsToCompletelyDelete = allPodIds.diff(usedPods.keySet)
          ScanDone(appsToCompletelyDelete, appVersionsToDelete,
            podsToCompletelyDelete, podVersionsToDelete, rootsToDelete)
        }.recover {
          case NonFatal(e) =>
            logger.error(s"Error while scanning for unused apps and pods ${Option(e.getMessage).getOrElse("")}: ", e)
            ScanDone()
        }
      }
      .fold(ScanDone()) {
        case (acc, scan) =>
          acc ++ scan
      }
      .runWith(Sink.head)
  }
}

private[storage] trait CompactBehavior[K, C, S] extends StrictLogging { this: FSM[State, Data] with ScanBehavior[K, C, S] =>
  val maxVersions: Int
  val cleaningInteveral: FiniteDuration
  val appRepository: AppRepositoryImpl[K, C, S]
  val podRepository: PodRepositoryImpl[K, C, S]
  val groupRepository: StoredGroupRepositoryImpl[K, C, S]
  val self: ActorRef

  when(Compacting) {
    case Event(RunGC, blocked: BlockedEntities) =>
      stay using blocked.copy(gcRequested = true)
    case Event(CompactDone, blocked: BlockedEntities) =>
      blocked.promises.foreach(_.success(Done))
      if (blocked.gcRequested) {
        scan().pipeTo(self)
        goto(Scanning) using UpdatedEntities()
      } else {
        if (cleaningInteveral <= 0.millis) {
          goto(ReadyForGc) using EmptyData
        } else {
          goto(Resting) using EmptyData
        }
      }
    case Event(StoreApp(appId, Some(version), promise), blocked: BlockedEntities) =>
      if (blocked.appsDeleting.contains(appId) ||
        blocked.appVersionsDeleting.get(appId).fold(false)(_.contains(version))) {
        stay using blocked.copy(promises = promise :: blocked.promises)
      } else {
        promise.success(Done)
        stay
      }
    case Event(StoreApp(appId, _, promise), blocked: BlockedEntities) =>
      if (blocked.appsDeleting.contains(appId)) {
        stay using blocked.copy(promises = promise :: blocked.promises)
      } else {
        promise.success(Done)
        stay
      }
    case Event(StorePod(podId, Some(version), promise), blocked: BlockedEntities) =>
      if (blocked.podsDeleting.contains(podId) ||
        blocked.podVersionsDeleting.get(podId).fold(false)(_.contains(version))) {
        stay using blocked.copy(promises = promise :: blocked.promises)
      } else {
        promise.success(Done)
        stay
      }
    case Event(StorePod(podId, _, promise), blocked: BlockedEntities) =>
      if (blocked.podsDeleting.contains(podId)) {
        stay using blocked.copy(promises = promise :: blocked.promises)
      } else {
        promise.success(Done)
        stay
      }
    case Event(StoreRoot(root, promise), blocked: BlockedEntities) =>
      // the last case could be optimized to actually check the versions...
      if (blocked.rootsDeleting.contains(root.version) ||
        blocked.appsDeleting.intersect(root.transitiveAppIds.keySet).nonEmpty ||
        blocked.appVersionsDeleting.keySet.intersect(root.transitiveAppIds.keySet).nonEmpty) {
        stay using blocked.copy(promises = promise :: blocked.promises)
      } else {
        promise.success(Done)
        stay
      }
    case Event(StorePlan(plan, promise), blocked: BlockedEntities) =>
      val promise1 = Promise[Done]()
      val promise2 = Promise[Done]()
      self ! StoreRoot(StoredGroup(plan.original), promise1)
      self ! StoreRoot(StoredGroup(plan.target), promise2)
      promise.completeWith(Future.sequence(Seq(promise1.future, promise2.future)).map(_ => Done))
      stay
  }

  def compact(appsToDelete: Set[PathId], appVersionsToDelete: Map[PathId, Set[OffsetDateTime]],
    podsToDelete: Set[PathId], podVersionsToDelete: Map[PathId, Set[OffsetDateTime]],
    rootVersionsToDelete: Set[OffsetDateTime]): Future[CompactDone] = {
    async { // linter:ignore UnnecessaryElseBranch
      if (rootVersionsToDelete.nonEmpty) {
        logger.info(s"Deleting Root Versions ${rootVersionsToDelete.mkString(", ")} as nothing refers to them anymore.")
      }
      if (appsToDelete.nonEmpty) {
        logger.info(s"Deleting Applications: (${appsToDelete.mkString(", ")}) as no roots refer to them")
      }
      if (appVersionsToDelete.nonEmpty) {
        logger.info("Deleting Application Versions " +
          s"(${appVersionsToDelete.map { case (id, v) => id -> v.mkString("[", ", ", "]") }.mkString(", ")}) as no roots refer to them" +
          " and they exceeded max versions")
      }
      if (podsToDelete.nonEmpty) {
        logger.info(s"Deleting Pods: (${podsToDelete.mkString(", ")}) as no roots refer to them")
      }
      if (podVersionsToDelete.nonEmpty) {
        logger.info("Deleting Pod Versions" +
          s"(${podVersionsToDelete.map { case (id, v) => id -> v.mkString("[", ", ", "]") }.mkString(", ")} as no roots refer to them" +
          " and they exceed max versions")
      }
      val appsDeletion = Source(appsToDelete)
        .mapAsync(1)(appRepository.delete)
      val appsVersionsDeletion = Source(appVersionsToDelete)
        .mapConcat { case (id, versions) => versions.map(v => v -> id) }
        .mapAsync(1){ case (version, id) => appRepository.deleteVersion(id, version) }
      val podsDeletion = Source(podsToDelete)
        .mapAsync(1)(podRepository.delete)
      val podsVersionsDeletion = Source(podVersionsToDelete)
        .mapConcat { case (id, versions) => versions.map(v => v -> id) }
        .mapAsync(1){ case (version, id) => podRepository.deleteVersion(id, version) }
      val rootVersionsDeletion = Source(rootVersionsToDelete)
        .mapAsync(1)(groupRepository.deleteRootVersion)
      val deletionProcess = (appsDeletion ++
        appsVersionsDeletion ++
        podsDeletion ++
        podsVersionsDeletion ++
        rootVersionsDeletion)
        .runWith(Sink.ignore)
      await(deletionProcess)
      CompactDone
    }.recover {
      case NonFatal(e) =>
        logger.error(s"While deleting unused objects ${Option(e.getMessage).getOrElse("")} encountered an error: ", e)
        CompactDone
    }
  }
}

object GcActor {
  private[storage] sealed trait State extends Product with Serializable
  case object Resting extends State
  case object ReadyForGc extends State
  case object Scanning extends State
  case object Compacting extends State

  val ScanIntervalTimerName = "scan-interval-time"

  private[storage] sealed trait Data extends Product with Serializable
  case object EmptyData extends Data
  case class UpdatedEntities(
      appsStored: Set[PathId] = Set.empty,
      appVersionsStored: Map[PathId, Set[OffsetDateTime]] = Map.empty.withDefaultValue(Set.empty),
      podsStored: Set[PathId] = Set.empty,
      podVersionsStored: Map[PathId, Set[OffsetDateTime]] = Map.empty.withDefaultValue(Set.empty),
      rootsStored: Set[OffsetDateTime] = Set.empty,
      gcRequested: Boolean = false) extends Data
  case class BlockedEntities(
      appsDeleting: Set[PathId] = Set.empty,
      appVersionsDeleting: Map[PathId, Set[OffsetDateTime]] = Map.empty.withDefaultValue(Set.empty),
      podsDeleting: Set[PathId] = Set.empty,
      podVersionsDeleting: Map[PathId, Set[OffsetDateTime]] = Map.empty.withDefaultValue(Set.empty),
      rootsDeleting: Set[OffsetDateTime] = Set.empty,
      promises: List[Promise[Done]] = List.empty,
      gcRequested: Boolean = false) extends Data

  def props[K, C, S](
    metrics: Metrics,
    deploymentRepository: DeploymentRepositoryImpl[K, C, S],
    groupRepository: StoredGroupRepositoryImpl[K, C, S],
    appRepository: AppRepositoryImpl[K, C, S],
    podRepository: PodRepositoryImpl[K, C, S],
    maxVersions: Int,
    scanBatchSize: Int,
    cleaningInteveral: FiniteDuration)(implicit mat: Materializer, ctx: ExecutionContext): Props = {
    Props(new GcActor[K, C, S](metrics, deploymentRepository, groupRepository, appRepository, podRepository, maxVersions, scanBatchSize, cleaningInteveral))
  }

  def apply[K, C, S](
    name: String,
    metrics: Metrics,
    deploymentRepository: DeploymentRepositoryImpl[K, C, S],
    groupRepository: StoredGroupRepositoryImpl[K, C, S],
    appRepository: AppRepositoryImpl[K, C, S],
    podRepository: PodRepositoryImpl[K, C, S],
    maxVersions: Int,
    scanBatchSize: Int,
    cleaningInteveral: FiniteDuration)(implicit
    mat: Materializer,
    ctx: ExecutionContext,
    actorRefFactory: ActorRefFactory): ActorRef = {
    actorRefFactory.actorOf(props(metrics, deploymentRepository, groupRepository,
      appRepository, podRepository, maxVersions, scanBatchSize, cleaningInteveral), name)
  }

  sealed trait Message extends Product with Serializable
  case class ScanDone(
      appsToDelete: Set[PathId] = Set.empty,
      appVersionsToDelete: Map[PathId, Set[OffsetDateTime]] = Map.empty,
      podsToDelete: Set[PathId] = Set.empty,
      podVersionsToDelete: Map[PathId, Set[OffsetDateTime]] = Map.empty,
      rootVersionsToDelete: Set[OffsetDateTime] = Set.empty) extends Message {
    def isEmpty = appsToDelete.isEmpty && appVersionsToDelete.isEmpty && rootVersionsToDelete.isEmpty
    def ++(that: ScanDone): ScanDone = ScanDone(
      appsToDelete ++ that.appsToDelete,
      that.appVersionsToDelete.foldLeft(appVersionsToDelete) {
        case (acc, thatValue @ (thatPathId, thatVersions)) =>
          acc.get(thatPathId) match {
            case Some(existingVersions) => acc.updated(thatPathId, existingVersions ++ thatVersions)
            case None => acc + thatValue
          }
      },
      podsToDelete ++ that.podsToDelete,
      that.podVersionsToDelete.foldLeft(podVersionsToDelete) {
        case (acc, thatValue @ (thatPathId, thatVersions)) =>
          acc.get(thatPathId) match {
            case Some(existingVersions) => acc.updated(thatPathId, existingVersions ++ thatVersions)
            case None => acc + thatValue
          }
      },
      rootVersionsToDelete ++ that.rootVersionsToDelete
    )
  }
  case object RunGC extends Message
  sealed trait CompactDone extends Message
  case object CompactDone extends CompactDone
  case object WakeUp extends Message

  sealed trait StoreEntity extends Message {
    val promise: Promise[Done]
  }
  object StoreEntity {
    def unapply(se: StoreEntity): Option[Promise[Done]] = Some(se.promise)
  }
  case class StorePod(podId: PathId, version: Option[OffsetDateTime], promise: Promise[Done]) extends StoreEntity
  case class StoreApp(appId: PathId, version: Option[OffsetDateTime], promise: Promise[Done]) extends StoreEntity
  case class StoreRoot(root: StoredGroup, promise: Promise[Done]) extends StoreEntity
  case class StorePlan(plan: DeploymentPlan, promise: Promise[Done]) extends StoreEntity
}
