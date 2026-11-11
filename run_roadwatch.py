"""Start RoadWatch on this computer. No cloud account or API key required."""
import argparse

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RoadWatch calibrated hazard dashboard")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    import uvicorn
    print(f"RoadWatch: http://127.0.0.1:{args.port}")
    uvicorn.run("roadwatch.app:app", host="127.0.0.1", port=args.port, log_level="info")
