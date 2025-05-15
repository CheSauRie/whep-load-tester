#!/usr/bin/env python3
"""
whep_stress_test.py - Stress-test WHEP WebRTC streams with metrics and proper SDP/ICE exchange

Usage:
    python whep_stress_test.py --url <WHEP_URL> --sessions 100 --concurrency 10 --duration 60 --stats-interval 10 --answer-method PUT
"""
import asyncio
import argparse
import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription

async def fetch_offer(session: aiohttp.ClientSession, url: str, session_id: int, offer_method: str) -> str:
    """
    Fetch SDP offer from WHEP endpoint using specified HTTP method.
    """
    async with session.request(offer_method, url) as response:
        response.raise_for_status()
        offer = await response.text()
        print(f"[Session {session_id}] Received offer SDP via {offer_method}:\n{offer}\n")
        return offer

async def send_answer(session: aiohttp.ClientSession, url: str, sdp: str, session_id: int, answer_method: str) -> None:
    """
    Send SDP answer back to the WHEP endpoint using specified HTTP method/content-type.
    """
    print(f"[Session {session_id}] Sending answer SDP via {answer_method}:\n{sdp}\n")
    headers = {'Content-Type': 'application/sdp'}
    async with session.request(answer_method, url, data=sdp.encode(), headers=headers) as response:
        if response.status != 200:
            print(f"[Session {session_id}] Answer {answer_method} failed: HTTP {response.status}")
        else:
            print(f"[Session {session_id}] Answer {answer_method} succeeded")

async def collect_stats(pc: RTCPeerConnection, interval: int, duration: int, session_id: int) -> None:
    """
    Periodically collect and print basic WebRTC stats.
    """
    iterations = max(1, duration // interval)
    for _ in range(iterations):
        await asyncio.sleep(interval)
        stats = await pc.getStats()
        inbound = [s for s in stats.values() if s.type == 'inbound-rtp']
        if inbound:
            for report in inbound:
                print(f"[Session {session_id}] stats: bytesReceived={report.bytesReceived}, packetsLost={report.packetsLost}, jitter={getattr(report, 'jitter', None)}, rtt={getattr(report, 'roundTripTime', None) or getattr(report, 'roundTripTimeMeasurements', None)}")
        else:
            print(f"[Session {session_id}] No inbound-rtp reports found")

async def run_session(url: str, duration: int, stats_interval: int, session_id: int, offer_method: str, answer_method: str) -> None:
    """
    Establish one WebRTC session, exchange SDP/ICE, collect stats, hold for `duration` seconds.
    """
    async with aiohttp.ClientSession() as session:
        # 1. Get offer SDP
        offer_sdp = await fetch_offer(session, url, session_id, offer_method)

        # 2. Setup PeerConnection and set remote description
        pc = RTCPeerConnection()
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=offer_sdp, type="offer")
        )

        # 3. Create answer and set local description
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        # 4. Wait for ICE gathering to complete
        while pc.iceGatheringState != 'complete':
            await asyncio.sleep(0.1)

        answer_sdp = pc.localDescription.sdp
        await send_answer(session, url, answer_sdp, session_id, answer_method)

        # 5. Start stats collection task
        stats_task = asyncio.create_task(collect_stats(pc, stats_interval, duration, session_id))

        # 6. Keep session alive
        await asyncio.sleep(duration)

        # 7. Cleanup
        stats_task.cancel()
        try:
            await stats_task
        except asyncio.CancelledError:
            pass
        await pc.close()
        print(f"[Session {session_id}] Closed")

async def run_stress(url: str, sessions: int, concurrency: int, duration: int, stats_interval: int, offer_method: str, answer_method: str) -> None:
    """
    Launch multiple WebRTC sessions concurrently for stress testing with metrics.
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded_session(i: int):
        async with semaphore:
            print(f"[+] Starting session #{i}")
            try:
                await run_session(url, duration, stats_interval, i, offer_method, answer_method)
                print(f"[+] Session #{i} completed successfully")
            except Exception as e:
                print(f"[!] Session #{i} error: {e}")

    tasks = [asyncio.create_task(bounded_session(i)) for i in range(1, sessions+1)]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="WHEP WebRTC stress test tool with full SDP/ICE exchange, customizable methods, and stats collection"
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
    parser.add_argument(
        "--offer-method", choices=["GET", "OPTIONS"], default="GET",
        help="HTTP method to fetch SDP offer"
    )
    parser.add_argument(
        "--answer-method", choices=["POST", "PUT"], default="POST",
        help="HTTP method to send SDP answer"
    )
    args = parser.parse_args()

    try:
        asyncio.run(run_stress(
            args.url, args.sessions, args.concurrency, args.duration, args.stats_interval,
            args.offer_method, args.answer_method
        ))
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user")
