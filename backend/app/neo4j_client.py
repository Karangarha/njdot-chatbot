from typing import Optional

from langchain_neo4j import Neo4jGraph

from .config import config


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
            )
            print("OK Neo4j client initialized")

        return cls._instance

    @classmethod
    def test_connection(cls) -> bool:
        """Test database connection."""
        try:
            graph = cls.get_graph()
            graph.query("RETURN 1 AS ok")
            print("OK Neo4j connection successful")
            return True
        except Exception as e:
            print(f"FAIL Neo4j connection failed: {str(e)}")
            return False


# Convenience function
def get_neo4j() -> Neo4jGraph:
    """Get Neo4j graph client instance."""
    return Neo4jClient.get_graph()


if __name__ == "__main__":
    # Test Neo4j connection
    print("-- Testing Neo4j connection...")
    Neo4jClient.test_connection()
