from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_KEY

_client: Client = None


def get_client() -> Client:
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError(
                "SUPABASE_URL / SUPABASE_KEY not set. Copy .env.example to .env "
                "and fill in your project's URL and service_role key."
            )
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client
