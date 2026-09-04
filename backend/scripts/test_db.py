import os
from dotenv import load_dotenv
from supabase import create_client, Client

# Load environment variables
load_dotenv()

def test_connection():
    """Test basic Supabase connection"""
    print("🔍 Testing Supabase connection...")

    # Get credentials from .env
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")

    if not url or not key:
        print(
            "❌ ERROR: Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env file"
        )
        return False

    print(f"📡 Connecting to: {url}")

    try:
        # Create Supabase client
        supabase: Client = create_client(url, key)

        # Test 1: Query chunks table (should be empty)
        print("\n✅ Test 1: Querying chunks table...")
        result = supabase.table("chunks").select("*").limit(5).execute()
        print(f"   Found {len(result.data)} chunks (expected 0 for new database)")

        # Test 2: Query rate_limits table
        print("\n✅ Test 2: Querying rate_limits table...")
        result = supabase.table("rate_limits").select("*").limit(5).execute()
        print(f"   Found {len(result.data)} rate limit records (expected 0)")

        # Test 3: Query bdc_section_map table
        print("\n✅ Test 3: Querying bdc_section_map table...")
        result = supabase.table("bdc_section_map").select("*").limit(5).execute()
        print(f"   Found {len(result.data)} BDC mappings (expected 0)")

        print("\n🎉 SUCCESS! Database connection works!")
        print("✅ All tables accessible")
        print("\n📋 Next steps:")
        print("   1. Start building the ingestion pipeline")
        print("   2. Create pdf_parser.py")
        print("   3. Ingest your first PDF")

        return True

    except Exception as e:
        print(f"\n❌ ERROR: {str(e)}")
        print("\n🔧 Troubleshooting:")
        print(
            "   1. Check your .env file has correct SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY"
        )
        print("   2. Verify you ran the database schema SQL in Supabase SQL Editor")
        print("   3. Check the tables exist in Supabase Table Editor")
        return False

if __name__ == "__main__":
    test_connection()
