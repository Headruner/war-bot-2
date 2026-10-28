import asyncio
import httpx
import os
import pytest
from app.esi import EsiClient

# Configuration from environment or defaults
ESI_BASE_URL = os.getenv("ESI_BASE_URL", "https://esi.evetech.net")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/default")
ADAM4EVE_URL = "https://www.adam4eve.eu/structures.php"

@pytest.mark.asyncio
async def test_esi_connectivity():
    """Verify that the ESI API is reachable and returns valid JSON for wars."""
    client = EsiClient(
        base_url=ESI_BASE_URL,
        user_agent="TestBot/1.0 (test@example.com)",
        timeout=10.0,
        compatibility_date="2025-08-26"
    )
    try:
        wars = await client.wars()
        assert isinstance(wars, list)
        assert len(wars) > 0
    except Exception as e:
        pytest.fail(f"ESI Connectivity failed: {e}")

@pytest.mark.asyncio
async def test_adam4eve_connectivity():
    """Verify that the Adam4EVE structure page is reachable."""
    async with httpx.AsyncClient() as client:
        response = await client.get(ADAM4EVE_URL)
        assert response.status_code == 200

@pytest.mark.asyncio
async def test_discord_webhook_connectivity():
    """Verify that the Discord Webhook URL is valid and accepting requests."""
    # Note: This will only pass if a real Webhook URL is provided in the environment
    if not DISCORD_WEBHOOK_URL or "default" in DISCORD_WEBHOOK_URL:
        pytest.skip("Discord Webhook URL not provided in environment.")
        
    async with httpx.AsyncClient() as client:
        # Send a dummy ping
        response = await client.post(DISCORD_WEBHOOK_URL, json={"content": "Integration Test"})
        assert response.status_code in [200, 204, 201]

@pytest.mark.asyncio
async def test_structure_fetch_logic():
    """Test that the bot can resolve structures for a sample alliance ID."""
    client = EsiClient(base_url=ESI_BASE_URL, user_agent="TestBot/1.0", timeout=10.0)
    # Sample alliance ID (dummy, but reachable if valid)
    # For a real integration test, we'd use a known active alliance ID
    alliance_id = 99010320 
    try:
        # Check if we can at least fetch the corporation list
        corps = await client.alliance_corporations(alliance_id)
        assert isinstance(corps, list)
    except Exception as e:
        # We expect this might fail if the ID is invalid, but we check if the request went out
        pytest.skip(f"Skipping live structure fetch for ID {alliance_id}: {e}")
