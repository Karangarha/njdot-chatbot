'''import sys
from pathlib import Path

# Add backend directory to sys.path if run directly as a script
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))'''

from supabase import create_client, Client
from typing import Optional
from .config import config


class Database:
    """Singleton Supabase database client."""

    _instance: Optional[Client] = None

    @classmethod
    def get_client(cls) -> Client:
        """Get or create Supabase client."""
        if cls._instance is None:
            if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
                raise ValueError(
                    "Missing Supabase credentials. "
                    "Check SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env file"
                )

            cls._instance = create_client(
                config.SUPABASE_URL,
                config.SUPABASE_SERVICE_ROLE_KEY,
            )
            print("OK Supabase client initialized")

        return cls._instance

    @classmethod
    def test_connection(cls) -> bool:
        """Test database connection."""
        try:
            client = cls.get_client()
            client.table("chunks").select("id").limit(1).execute()
            print("OK Database connection successful")
            return True
        except Exception as e:
            print(f"FAIL Database connection failed: {str(e)}")
            return False


# Convenience function
def get_db() -> Client:
    """Get database client instance."""
    return Database.get_client()



if __name__ == "__main__":
    # Test database connection
    print("-- Testing database connection...")
    Database.test_connection()
