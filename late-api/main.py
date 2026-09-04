from fastapi import FastAPI
from typing import Optional
from late_client.ssh import LateClient
from late_client.extractors import extract_leaderboards, extract_status

app = FastAPI(
    title="late.sh Client API",
    description="A decoupled, client-side API that exposes data from the late.sh terminal interface by rendering and scraping the VT100 TUI."
)

@app.get("/leaderboards")
def get_leaderboards(host: str = "late.sh", port: int = 22, user: Optional[str] = None):
    """
    Connect to late.sh, navigate to the leaderboards, parse the terminal output,
    and return the data as structured JSON.
    """
    with LateClient(host=host, port=port, user=user) as client:
        # Wait for splash screen to finish rendering and read it into the buffer
        client.read_raw(timeout=1.0)
        
        # Press '6' to open the leaderboards
        client.send_keys('6')
        
        # Wait for the leaderboard data to be transmitted and rendered
        client.read_raw(timeout=2.0)
        
        # Translate VT100 sequences into a final string grid
        lines = client.get_screen()
        
        # Spatially extract the leaderboards
        boards = extract_leaderboards(lines)
        
        return boards

@app.get("/status")
def get_status(host: str = "late.sh", port: int = 22, user: Optional[str] = None):
    """
    Connect to late.sh to retrieve the logged in user's status regarding
    their pet (water/food needs) and active multiplayer games (turn status).
    """
    with LateClient(host=host, port=port, user=user) as client:
        # 1. Wait for Home screen to load (contains pet strip)
        client.read_raw(timeout=1.5)
        screen1_lines = client.get_screen()
        
        # 2. Press Ctrl+G to open the Lobby modal (contains multiplayer games)
        client.send_keys('\x07') # Ctrl+G
        
        # Wait for the Lobby modal to render
        client.read_raw(timeout=1.5)
        screen2_lines = client.get_screen()
        
        # 3. Navigate to Arcade
        client.send_keys('') # Esc to close Lobby modal
        client.read_raw(timeout=0.5)
        client.send_keys('2') # 2 for Arcade
        client.read_raw(timeout=1.5)
        screen3_lines = client.get_screen()

        # 4. Extract status from all three screens
        status_data = extract_status(screen1_lines, screen2_lines, screen3_lines)
        
        return status_data

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
