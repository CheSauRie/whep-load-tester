#!/usr/bin/env python3
"""
whep_stress_test.py - Stress-test WHEP WebRTC streams with metrics collection

Usage:
    python whep_stress_test.py --url <WHEP_URL> --sessions 100 --concurrency 10 --duration 60 --stats-interval 10
"""
import asyncio
import argparse
import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription

async def fetch_offer(session: aiohttp.ClientSession, url: str) -> str:
    """
    Fetch SDP offer from WHEP endpoint.
    """
    async with session.get(url) as response:
        response.raise_for_status()
        return await response.text()

async def send_answer(session: aiohttp.ClientSession, url: str, sdp: str) -> None:
    """
    Send SDP answer back to the WHEP endpoint.
    """
    async with session.post(url, data=sdp) as response:
        if response.status != 200:
            print(f"[!] Answer POST failed: HTTP {response.status}")

async def collect_stats(pc: RTCPeerConnection, interval: int, duration: int, session_id: int) -> None:
    """
    Periodically collect and print basic WebRTC stats.
    """
    iterations = max(1, duration // interval)
    for i in range(iterations):
        await asyncio.sleep(interval)
        stats = await pc.getStats()
        # extract common metrics
        inbound = [s for s in stats.values() if s.type == 'inbound-rtp']
        if inbound:
            for report in inbound:
                bytes_received = report.bytesReceived
                packets_lost = report.packetsLost
                jitter = getattr(report, 'jitter', None)
                rtt = getattr(report, 'roundTripTime', None) or getattr(report, 'roundTripTimeMeasurements', None)
                print(f"[Session {session_id}] stats: bytesReceived={bytes_received}, packetsLost={packets_lost}, jitter={jitter}, rtt={rtt}")
        else:
            print(f"[Session {session_id}] No inbound-rtp reports found")

async def run_session(url: str, duration: int, stats_interval: int, session_id: int) -> None:
    """
    Establish one WebRTC session, collect stats, and keep it open for `duration` seconds.
    """
    async with aiohttp.ClientSession() as session:
        # 1. Get offer SDP
        offer_sdp = await fetch_offer(session, url)

        # 2. Setup PeerConnection
        pc = RTCPeerConnection()
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=offer_sdp, type="offer")
        )

        # 3. Create and send answer SDP
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await send_answer(session, url, pc.localDescription.sdp)

        # 4. Start periodic stats collection
        stats_task = asyncio.create_task(collect_stats(pc, stats_interval, duration, session_id))

        # 5. Keep streaming for the given duration
        await asyncio.sleep(duration)

        # 6. Cancel stats task and close connection
        stats_task.cancel()
        try:
            await stats_task
        except asyncio.CancelledError:
            pass
        await pc.close()

async def run_stress(url: str, sessions: int, concurrency: int, duration: int, stats_interval: int) -> None:
    """
    Launch multiple WebRTC sessions concurrently for stress testing with metrics.
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded_session(i: int):
        async with semaphore:
            print(f"[+] Starting session #{i}")
            try:
                await run_session(url, duration, stats_interval, i)
                print(f"[+] Session #{i} completed")
            except Exception as e:
                print(f"[!] Session #{i} error: {e}")

    tasks = [asyncio.create_task(bounded_session(i)) for i in range(1, sessions+1)]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="WHEP WebRTC stress test tool with stats collection"
    )
    parser.add_argument(
        "--url", required=True, help="WHEP endpoint URL"
    )
    parser.add_argument(
        "--sessions", type=int, default=100,
        help="Total number of sessions to open"
    )
    parser.add_argument(
        "--concurrency", type=int, default=10,
        help="Number of concurrent sessions"
    )
    parser.add_argument(
        "--duration", type=int, default=60,
        help="Duration to keep each session open (seconds)"
    )
    parser.add_argument(
        "--stats-interval", type=int, default=10,
        help="Interval in seconds between stats collections"
    )
    args = parser.parse_args()

    try:
        asyncio.run(run_stress(
            args.url, args.sessions, args.concurrency, args.duration, args.stats_interval
        ))
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user")
