import logging, time
from contextlib import contextmanager
from typing import Any, Generator, Optional, Tuple, Type
from ..api import VectorDB, DBCaseConfig, IndexType
from .config import MemoryDBIndexConfig
import redis
from redis import Redis
from redis.cluster import RedisCluster
from redis.commands.search.field import TagField, VectorField, NumericField
from redis.commands.search.indexDefinition import IndexDefinition, IndexType
from redis.commands.search.query import Query
import numpy as np
import threading
import queue


log = logging.getLogger(__name__)
INDEX_NAME = "index"    # Vector Index Name

class MemoryDB(VectorDB):
    def __init__(
            self,
            dim: int,
            db_config: dict,
            db_case_config: MemoryDBIndexConfig,
            drop_old: bool = False,
            **kwargs
        ):

        self.db_config = db_config
        self.case_config = db_case_config
        self.collection_name = INDEX_NAME
        self.target_nodes = RedisCluster.RANDOM if not self.db_config["cmd"] else None
        self.insert_batch_size = db_case_config.insert_batch_size
        self.ingestion_thread_count = db_case_config.ingestion_thread_count
        self.no_content = db_case_config.no_content
        self.dbsize = kwargs.get("num_rows")

        # Create a MemoryDB connection, if db has password configured, add it to the connection here and in init():
        log.info(f"Establishing connection to: {self.db_config}")
        conn = self.get_client(primary=True)
        log.info(f"Connection established: {conn}")
        log.info(conn.execute_command("INFO server"))

        if drop_old:
            try:
                log.info(f"MemoryDB client getting info for: {INDEX_NAME}")
                info = conn.ft(INDEX_NAME).info()
                log.info(f"Index info: {info}")
            except redis.exceptions.ResponseError as e:
                log.error(e)
                drop_old = False
                log.info(f"MemoryDB client drop_old collection: {self.collection_name}")
            
            log.info("Executing FLUSHALL")
            conn.flushall()

            # Since the default behaviour of FLUSHALL is asynchronous, wait for db to be empty
            self.wait_until(self.wait_for_empty_db, 3, "", conn)
            if not self.db_config["cmd"]:
                replica_clients = self.get_client(replicas=True)
                for rc, host in replica_clients:
                    self.wait_until(self.wait_for_empty_db, 3, "", rc)
                    log.debug(f"Flushall done in the host: {host}")
                    rc.close()
        
        self.make_index(dim, conn)
        conn.close()
        conn = None

    def make_index(self, vector_dimensions: int, conn: redis.Redis):
        try:
            # check to see if index exists
            conn.ft(INDEX_NAME).info()
        except Exception as e:
            log.warn(f"Error getting info for index '{INDEX_NAME}': {e}")
            index_param = self.case_config.index_param()
            search_param = self.case_config.search_param()
            vector_parameters = {  # Vector Index Type: FLAT or HNSW
                "TYPE": "FLOAT32",
                "DIM": vector_dimensions,  # Number of Vector Dimensions
                "DISTANCE_METRIC": index_param["metric"],  # Vector Search Distance Metric
            }
            if index_param["m"]:
                vector_parameters["M"] = index_param["m"]
            if index_param["ef_construction"]:
                vector_parameters["EF_CONSTRUCTION"] = index_param["ef_construction"]
            if search_param["ef_runtime"]:
                vector_parameters["EF_RUNTIME"] = search_param["ef_runtime"]

            schema = (
                TagField("id"),                   
                NumericField("metadata"),              
                VectorField("vector",   # Vector Field Name
                    "HNSW", vector_parameters
                ),
            )

            definition = IndexDefinition(index_type=IndexType.HASH)
            rs = conn.ft(INDEX_NAME)
            rs.create_index(schema, definition=definition)
    
    def get_client(self, **kwargs):
        """
        Gets either cluster connection or normal connection based on `cmd` flag.
        CMD stands for Cluster Mode Disabled and is a "mode".
        """
        if not self.db_config["cmd"]:
            # Cluster mode enabled

            client = RedisCluster(
                host=self.db_config["host"],
                port=self.db_config["port"],
                ssl=self.db_config["ssl"],
                password=self.db_config["password"],
                ssl_ca_certs=self.db_config["ssl_ca_certs"],
                ssl_cert_reqs=None,
            )

            # Ping all nodes to create a connection
            client.execute_command("PING", target_nodes=RedisCluster.ALL_NODES)
            replicas = client.get_replicas()

            if len(replicas) > 0:
                # FT.SEARCH is a keyless command, use READONLY for replica connections
                client.execute_command("READONLY", target_nodes=RedisCluster.REPLICAS)

            if kwargs.get("primary", False):
                client = client.get_primaries()[0].redis_connection

            if kwargs.get("replicas", False):
                # Return client and host name for each replica
                return [(c.redis_connection, c.host) for c in replicas]

        else:
            client = Redis(
                host=self.db_config["host"],
                port=self.db_config["port"],
                db=0,
                ssl=self.db_config["ssl"],
                password=self.db_config["password"],
                ssl_ca_certs=self.db_config["ssl_ca_certs"],
                ssl_cert_reqs=None,
            )
            client.execute_command("PING")
        return client


    def get_client_pool(self, **kwargs):
        if not self.db_config["cmd"]:
            return None
        else:
            return redis.connection.ConnectionPool(
                host=self.db_config["host"],
                port=self.db_config["port"],
                db=0,
            )

    @contextmanager
    def init(self) -> Generator[None, None, None]:
        """ create and destory connections to database.

        Examples:
            >>> with self.init():
            >>>     self.insert_embeddings()
        """
        # Create a connection pool for loading, and a single client for searching.
        self.conn_pool = self.get_client_pool()
        self.conn = self.get_client()
        search_param = self.case_config.search_param()
        if search_param["ef_runtime"]:
            self.ef_runtime_str = f'EF_RUNTIME {search_param["ef_runtime"]}'
        else:
            self.ef_runtime_str = ""
        yield
        self.conn_pool.close()
        self.conn.close()
        self.conn_pool = None

    def ready_to_load(self) -> bool:
        pass

    def optimize(self) -> None:
        self._post_insert()

    def insert_embedding_batch(
        self,
        conn,
        embeddings,
        metadata,
        result_queue
    ):
        try:
            result_len = 0
            with conn.pipeline(transaction=False) as pipe:
                for i, embedding in enumerate(embeddings):
                    embedding = np.array(embedding).astype(np.float32)
                    pipe.hset(metadata[i], mapping = {
                        "id": str(metadata[i]),
                        "metadata": metadata[i], 
                        "vector": embedding.tobytes(),
                    })
                    # Execute the pipe so we don't keep too much in memory at once
                    if (i + 1) % self.insert_batch_size == 0:
                        pipe.execute()

                pipe.execute()
                result_len = i + 1
            result_queue.put(result_len)
        except Exception as e:
            print(e)
            result_queue.put(0)

    def split_list_into_parts(self, lst, num_parts):
        base_size = len(lst) // num_parts
        remainder = len(lst) % num_parts
        sizes = [base_size + 1 if i < remainder else base_size for i in range(num_parts)]
        result = []
        index = 0
        for size in sizes:
            result.append(lst[index:index + size])
            index += size
        return result

    def insert_embeddings(
        self,
        embeddings: list[list[float]],
        metadata: list[int],
        **kwargs: Any,
    ) -> Tuple[int, Optional[Exception]]:
        """Insert embeddings into the database.
        Should call self.init() first.
        """
        result_len = 0
        try:
            threads = []
            result_queue = queue.Queue()
            thread_count = self.ingestion_thread_count
            embedding_parts = self.split_list_into_parts(embeddings, thread_count)
            metadata_parts = self.split_list_into_parts(metadata, thread_count)
            for i in range(thread_count):
                conn = redis.Redis(connection_pool=self.conn_pool)
                thread = threading.Thread(target=self.insert_embedding_batch, args=(conn, embedding_parts[i], metadata_parts[i], result_queue,))
                threads.append(thread)
                thread.start()
            for thread in threads:
                thread.join()
            while not result_queue.empty():
                result_len += result_queue.get()
        except Exception as e:
            return 0, e
        
        return result_len, None
    
    def _post_insert(self):
        """Wait for indexing to finish"""
        client = self.get_client(primary=True)
        log.info("Waiting for background indexing to finish")
        args = (self.wait_for_no_activity, 5, "", client)
        self.wait_until(*args)
        if not self.db_config["cmd"]:
            replica_clients = self.get_client(replicas=True)
            for rc, host_name in replica_clients:
                args = (self.wait_for_no_activity, 5, "", rc)
                self.wait_until(*args)
                log.debug(f"Background indexing completed in the host: {host_name}")
                rc.close()
    
    def wait_until(
        self, condition, interval=5, message="Operation took too long", *args
    ):
        while not condition(*args):
            time.sleep(interval)
    
    def wait_for_no_activity(self, client: redis.RedisCluster | redis.Redis):
        return (
            client.info("search")["search_background_indexing_status"] == "NO_ACTIVITY"
        )
    
    def wait_for_empty_db(self, client: redis.RedisCluster | redis.Redis):
        return client.execute_command("DBSIZE") == 0
    
    def search_embedding(
        self,
        query: list[float],
        k: int = 10,
        filters: dict | None = None,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> (list[int]):
        assert self.conn is not None
        
        query_vector = np.array(query).astype(np.float32).tobytes()
        query_obj = Query(f"*=>[KNN {k} @vector $vec {self.ef_runtime_str}]").paging(0, k)
        query_params = {"vec": query_vector}
        
        if filters:
            # Removing '>=' from the id_value: '>=10000'
            metadata_value = filters.get("metadata")[2:]
            query_obj = Query(f"@metadata:[{metadata_value} +inf]=>[KNN {k} @vector $vec {self.ef_runtime_str}]").paging(0, k)

        if self.no_content:
            query_obj = query_obj.no_content()
        else:
            query_obj = query_obj.return_fields("id")
        res = self.conn.ft(INDEX_NAME).search(query_obj, query_params)
        return [int(doc["id"]) for doc in res.docs]
