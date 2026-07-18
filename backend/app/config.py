"""Configuration management for NJDOT Chatbot."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load base config from .env first.
# If .env.local exists in the same directory, load it afterwards with
# override=True so its values take precedence. This lets you point the
# backend at a test Supabase project just by creating .env.local, and
# switch back to production by deleting it.
_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")
_local_env = _BASE_DIR / ".env.local"
if _local_env.exists():
    load_dotenv(_local_env, override=True)
    print("[LOCAL] Using local database (.env.local)")


class Config:
    """Application configuration."""

    # Supabase
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_SERVICE_ROLE_KEY: str = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_KEY", "")
    )

    # OpenAI
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    CHAT_MODEL: str = os.getenv("CHAT_MODEL", "gpt-4o")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "openai")  # "openai" | "anthropic"

    # Environment
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")

    # Max concurrent LLM calls when evaluating a review's checklist
    # (app.compliance.eval_engine.evaluate_checks) — each check is one
    # structured-output call; running several at once cuts wall-clock review
    # time, but too high a value risks tripping provider rate limits.
    REVIEW_CHECK_CONCURRENCY: int = int(os.getenv("REVIEW_CHECK_CONCURRENCY", "8"))

    # Frontend origin for CORS (set to Vercel URL in production)
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:3000")

    # Supabase JWT secret — used to verify user tokens in conversations endpoints.
    # Find it at: Supabase Dashboard → Project Settings → API → JWT Secret
    SUPABASE_JWT_SECRET: str = os.getenv("SUPABASE_JWT_SECRET", "")

    # Paths
    BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATA_DIR: str = os.path.join(BASE_DIR, "data")
    RAW_PDFS_DIR: str = os.path.join(DATA_DIR, "raw_pdfs")

    # Embedding settings
    EMBEDDING_MODEL: str = "text-embedding-3-small"

    # Neo4j (local Desktop instance — see backend/app/neo4j_client.py)
    NEO4J_URI: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    NEO4J_USERNAME: str = os.getenv("NEO4J_USERNAME", "neo4j")
    NEO4J_PASSWORD: str = os.getenv("NEO4J_PASSWORD", "")
    NEO4J_DATABASE: str = os.getenv("NEO4J_DATABASE", "neo4j")

    @classmethod
    def validate(cls) -> bool:
        """Validate required configuration."""
        required = {
            "SUPABASE_URL": cls.SUPABASE_URL,
            "SUPABASE_SERVICE_ROLE_KEY": cls.SUPABASE_SERVICE_ROLE_KEY,
            "NEO4J_URI": cls.NEO4J_URI,
            "NEO4J_USERNAME": cls.NEO4J_USERNAME,
            "NEO4J_PASSWORD": cls.NEO4J_PASSWORD,
            "OPENAI_API_KEY": cls.OPENAI_API_KEY,
        }

        missing = [key for key, value in required.items() if not value]

        if missing:
            print(f"FAIL Missing required environment variables: {', '.join(missing)}")
            return False

        print("OK Configuration validated")
        return True

    @classmethod
    def print_config(cls) -> None:
        """Print current configuration (for debugging)."""
        print("\nCurrent Configuration:")
        print(f"   Environment: {cls.ENVIRONMENT}")
        print(f"   Supabase URL: {cls.SUPABASE_URL}")
        print(f"   OpenAI Key: {'Set' if cls.OPENAI_API_KEY else 'Not set'}")
        print(f"   Data Directory: {cls.DATA_DIR}")
        print(f"   PDFs Directory: {cls.RAW_PDFS_DIR}")
        print()


# Create singleton instance
config = Config()


if __name__ == "__main__":
    # Test configuration
    config.print_config()
    config.validate()
