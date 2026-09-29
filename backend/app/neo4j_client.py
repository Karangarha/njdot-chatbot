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
                # Connections are opened on demand and closed once they're
                # 3 min old, so none outlives Azure's ~4 min outbound idle
                # timeout. That timeout drops sockets silently (no RST), so a
                # liveness ping on a dropped socket just hangs -- in prod it
                # burned the whole 60s acquisition timeout and failed a review
                # after a ~6 min PDF-extraction gap. Nothing project-specific
                # lives on a connection: all data is keyed by projectId, so a
                # fresh connection sees exactly what the old one did.
                driver_config={
                    "max_connection_lifetime": 180,
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
