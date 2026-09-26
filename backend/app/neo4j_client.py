import logging
from typing import Optional

from langchain_neo4j import Neo4jGraph

from .config import config

logger = logging.getLogger(__name__)


class Neo4jClient:
    """Singleton LangChain Neo4j graph client."""

    _instance: Optional[Neo4jGraph] = None

    @classmethod
    def get_graph(cls) -> Neo4jGraph:
        """Get or create the Neo4jGraph client."""
        if cls._instance is None:
            if not config.NEO4J_URI or not config.NEO4J_PASSWORD:
                raise ValueError(
                    "Missing Neo4j credentials. "
                    "Check NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD in .env file"
                )

            cls._instance = Neo4jGraph(
                url=config.NEO4J_URI,
                username=config.NEO4J_USERNAME,
                password=config.NEO4J_PASSWORD,
                database=config.NEO4J_DATABASE,
                enhanced_schema=True,
                # This client is a process-wide singleton (see below), so its
                # driver's connection pool can sit idle for minutes between
                # requests. The neo4j driver's liveness_check_timeout defaults
                # to None (never verify a pooled connection before reuse), so
                # a connection silently killed by an intermediate NAT/idle
                # timeout (Azure's outbound NAT, Aura's own reaping, etc.)
                # looks fine in the pool until a query actually tries to use
                # it, raising SessionExpired/ServiceUnavailable -- reproduced
                # in production after a ~4min idle gap between two reviews.
                # A short liveness check makes the driver ping (and silently
                # replace) a connection that's been idle this long, before
                # ever handing it to a query.
                driver_config={
                    "liveness_check_timeout": 60,
                    # 01N51/01N52 ("relationship type / property does not
                    # exist") fire on every review for graph features a
                    # project has no data for yet -- expected, not
                    # actionable. Other notification classes still log.
                    "notifications_disabled_classifications": ["UNRECOGNIZED"],
                },
            )
            logger.info("Neo4j client initialized")

        return cls._instance

    @classmethod
    def test_connection(cls) -> bool:
        """Test Neo4j connection.

        Dev-time probe only: the running server never calls this (its sole
        caller is this module's own __main__ block). A Neo4j failure during a
        real request surfaces through the caller that raised, not from here.
        """
        try:
            graph = cls.get_graph()
            graph.query("RETURN 1 AS ok")
            logger.info("Neo4j connection successful")
            return True
        except Exception as e:
            logger.error("Neo4j connection failed: %s", e, exc_info=True)
            return False


# Convenience function
def get_neo4j() -> Neo4jGraph:
    """Get Neo4j graph client instance."""
    return Neo4jClient.get_graph()


if __name__ == "__main__":
    # Test Neo4j connection
    print("-- Testing Neo4j connection...")
    Neo4jClient.test_connection()
