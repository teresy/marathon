package mesosphere.marathon
package storage.repository

import java.util.UUID

import akka.Done
import mesosphere.AkkaUnitTest
import mesosphere.marathon.core.storage.repository.SingletonRepository
import mesosphere.marathon.core.storage.store.impl.cache.{LazyCachingPersistenceStore, LoadTimeCachingPersistenceStore}
import mesosphere.marathon.core.storage.store.impl.memory.InMemoryPersistenceStore
import mesosphere.marathon.core.storage.store.impl.zk.ZkPersistenceStore
import mesosphere.marathon.metrics.dummy.DummyMetrics
import mesosphere.marathon.util.ZookeeperServerTest
import mesosphere.util.state.FrameworkId

class SingletonRepositoryTest extends AkkaUnitTest with ZookeeperServerTest {
  val metrics = DummyMetrics

  def basic(name: String, createRepo: => SingletonRepository[FrameworkId]): Unit = {
    name should {
      "return none if nothing has been stored" in {
        val repo = createRepo
        repo.get().futureValue should be ('empty)
      }
      "delete should succeed if nothing has been stored" in {
        val repo = createRepo
        repo.delete().futureValue should be(Done)
      }
      "retrieve the previously stored value" in {
        val repo = createRepo
        val id = FrameworkId(UUID.randomUUID().toString)
        repo.store(id).futureValue
        repo.get().futureValue.value should equal(id)
      }
      "delete a previously stored value should unset the value" in {
        val repo = createRepo
        val id = FrameworkId(UUID.randomUUID().toString)
        repo.store(id).futureValue
        repo.delete().futureValue should be(Done)
        repo.get().futureValue should be ('empty)
      }
    }
  }

  def createInMemRepo(): FrameworkIdRepository = {
    val store = new InMemoryPersistenceStore(metrics)
    store.markOpen()
    FrameworkIdRepository.inMemRepository(store)
  }

  def createLoadTimeCachingRepo(): FrameworkIdRepository = {
    val cached = new LoadTimeCachingPersistenceStore(new InMemoryPersistenceStore(metrics))
    cached.markOpen()
    cached.preDriverStarts.futureValue
    FrameworkIdRepository.inMemRepository(cached)
  }

  def createZKRepo(): FrameworkIdRepository = {
    val store = new ZkPersistenceStore(metrics, zkClient())
    store.markOpen()
    FrameworkIdRepository.zkRepository(store)
  }

  def createLazyCachingRepo(): FrameworkIdRepository = {
    val store = LazyCachingPersistenceStore(metrics, new InMemoryPersistenceStore(metrics))
    store.markOpen()
    FrameworkIdRepository.inMemRepository(store)
  }

  behave like basic("InMemoryPersistence", createInMemRepo())
  behave like basic("ZkPersistence", createZKRepo())
  behave like basic("LoadTimeCachingPersistence", createLoadTimeCachingRepo())
  behave like basic("LazyCachingPersistence", createLazyCachingRepo())
}
