#!/usr/bin/env python3
"""
WebRTC WHEP Stress Test Tool
----------------------------
Công cụ này tạo nhiều kết nối WHEP đồng thời để stress test máy chủ WebRTC.
WHEP (WebRTC HTTP Egress Protocol) là một giao thức tiêu chuẩn hóa để thiết lập
các kết nối WebRTC thông qua HTTP.
"""

import argparse
import asyncio
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional

import aiohttp
import av
import matplotlib.pyplot as plt
import numpy as np
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.contrib.media import MediaPlayer, MediaRecorder
from aiortc.rtcrtpparameters import RTCRtpTransceiverDirection

# Thiết lập logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("stress_test.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("whep_stress_test")

@dataclass
class ConnectionStats:
    connection_id: str
    start_time: float
    setup_time: Optional[float] = None
    ice_connection_time: Optional[float] = None
    connection_state: str = "new"
    bytes_received: int = 0
    packets_received: int = 0
    packets_lost: int = 0
    jitter: float = 0.0
    frame_rate: float = 0.0
    rtt: float = 0.0
    error: Optional[str] = None

class WHEPClient:
    def __init__(
        self, 
        whep_url: str, 
        connection_id: str,
        ice_servers: List[Dict] = None, 
        record_path: Optional[str] = None,
        stats_interval: int = 5
    ):
        self.whep_url = whep_url
        self.connection_id = connection_id
        self.stats = ConnectionStats(connection_id=connection_id, start_time=time.time())
        self.pc = None
        self.session = None
        self.is_connected = False
        self.is_closed = False
        self.stats_interval = stats_interval
        self.record_path = record_path
        self.recorder = None
        
        # Khởi tạo cấu hình ICE servers
        self.rtc_config = RTCConfiguration(
            iceServers=ice_servers if ice_servers else [{"urls": ["stun:stun.l.google.com:19302"]}]
        )
        
    async def connect(self):
        """Thiết lập kết nối WHEP với máy chủ"""
        try:
            # Khởi tạo peer connection
            self.pc = RTCPeerConnection(configuration=self.rtc_config)
            
            # Tạo session HTTP để giao tiếp với máy chủ WHEP
            self.session = aiohttp.ClientSession()
            
            # Thêm audio và video transceivers cho kết nối WHEP
            # Lưu ý: WHEP thường là "receive-only" nên chúng ta chỉ cần thêm transceivers
            # mà không cần thực sự gửi media
            self.pc.addTransceiver("audio", direction="recvonly")
            self.pc.addTransceiver("video", direction="recvonly")
            
            # Thiết lập recorder nếu được yêu cầu
            if self.record_path:
                self.recorder = MediaRecorder(f"{self.record_path}/client_{self.connection_id}.mp4")
            
            # Đăng ký các event handlers
            @self.pc.on("track")
            async def on_track(track):
                logger.info(f"Client {self.connection_id}: Received {track.kind} track")
                if self.recorder:
                    self.recorder.addTrack(track)
                    await self.recorder.start()

            @self.pc.on("connectionstatechange")
            async def on_connectionstatechange():
                state = self.pc.connectionState
                self.stats.connection_state = state
                logger.info(f"Client {self.connection_id}: Connection state changed to {state}")
                
                if state == "connected":
                    self.is_connected = True
                    ice_time = time.time() - self.stats.start_time
                    self.stats.ice_connection_time = ice_time
                    logger.info(f"Client {self.connection_id}: ICE connected in {ice_time:.2f} seconds")
                    asyncio.create_task(self.collect_stats())
                    
                elif state == "failed" or state == "closed":
                    self.is_connected = False
                    if not self.is_closed:
                        await self.close(f"Connection state changed to {state}")
                        
            # Tạo offer
            offer = await self.pc.createOffer()
            await self.pc.setLocalDescription(offer)
            
            # Gửi offer đến máy chủ WHEP
            start_request_time = time.time()
            try:
                async with self.session.post(
                    self.whep_url,
                    data=self.pc.localDescription.sdp,
                    headers={
                        "Content-Type": "application/sdp",
                        "Accept": "application/sdp"
                    }
                ) as response:
                    if response.status != 201 and response.status != 200:
                        error_msg = f"WHEP server returned status code: {response.status}"
                        logger.error(f"Client {self.connection_id}: {error_msg}")
                        self.stats.error = error_msg
                        await self.close(error_msg)
                        return False
                    
                    # Lấy SDP answer từ response
                    sdp_answer = await response.text()
                    self.stats.setup_time = time.time() - start_request_time
                    
                    # Cài đặt remote description
                    await self.pc.setRemoteDescription(
                        RTCSessionDescription(sdp=sdp_answer, type="answer")
                    )
                    
                    logger.info(f"Client {self.connection_id}: WHEP negotiation completed in {self.stats.setup_time:.2f} seconds")
                    return True
                    
            except Exception as e:
                error_msg = f"HTTP request failed: {str(e)}"
                logger.error(f"Client {self.connection_id}: {error_msg}")
                self.stats.error = error_msg
                await self.close(error_msg)
                return False
                
        except Exception as e:
            error_msg = f"Failed to establish connection: {str(e)}"
            logger.error(f"Client {self.connection_id}: {error_msg}")
            self.stats.error = error_msg
            await self.close(error_msg)
            return False
    
    async def collect_stats(self):
        """Thu thập thống kê định kỳ từ kết nối"""
        while self.is_connected and not self.is_closed:
            try:
                # Thu thập thống kê từ các receivers
                for transceiver in self.pc.getTransceivers():
                    if transceiver.receiver and transceiver.receiver.track:
                        stats = await transceiver.receiver.getStats()
                        
                        for stat in stats.values():
                            if stat.type == "inbound-rtp":
                                # Cập nhật thống kê
                                if hasattr(stat, "bytesReceived"):
                                    self.stats.bytes_received = stat.bytesReceived
                                if hasattr(stat, "packetsReceived"):
                                    self.stats.packets_received = stat.packetsReceived
                                if hasattr(stat, "packetsLost"):
                                    self.stats.packets_lost = stat.packetsLost
                                if hasattr(stat, "jitter"):
                                    self.stats.jitter = stat.jitter
                                if hasattr(stat, "framesPerSecond"):
                                    self.stats.frame_rate = stat.framesPerSecond
                                    
                            elif stat.type == "remote-inbound-rtp":
                                if hasattr(stat, "roundTripTime"):
                                    self.stats.rtt = stat.roundTripTime
                
                # In thống kê ra log
                logger.debug(
                    f"Client {self.connection_id} stats: "
                    f"Bytes: {self.stats.bytes_received}, "
                    f"Packets: {self.stats.packets_received}, "
                    f"Lost: {self.stats.packets_lost}, "
                    f"Jitter: {self.stats.jitter:.3f}s, "
                    f"RTT: {self.stats.rtt:.3f}s"
                )
                
            except Exception as e:
                logger.error(f"Client {self.connection_id}: Error collecting stats: {str(e)}")
                
            await asyncio.sleep(self.stats_interval)
    
    async def close(self, reason: str = "Normal closure"):
        """Đóng kết nối và giải phóng tài nguyên"""
        if self.is_closed:
            return
            
        self.is_closed = True
        self.is_connected = False
        logger.info(f"Client {self.connection_id}: Closing connection. Reason: {reason}")
        
        if self.recorder and self.recorder.started:
            await self.recorder.stop()
            
        if self.pc:
            await self.pc.close()
            
        if self.session:
            await self.session.close()

class StressTest:
    def __init__(
        self,
        whep_url: str,
        total_clients: int,
        ramp_up_time: int,
        test_duration: int,
        ice_servers: List[Dict] = None,
        record_path: Optional[str] = None,
        stats_interval: int = 5
    ):
        self.whep_url = whep_url
        self.total_clients = total_clients
        self.ramp_up_time = ramp_up_time
        self.test_duration = test_duration
        self.ice_servers = ice_servers
        self.record_path = record_path
        self.stats_interval = stats_interval
        
        self.clients: Dict[str, WHEPClient] = {}
        self.all_stats: List[ConnectionStats] = []
        self.start_time = None
        self.is_running = False
        
    async def run(self):
        """Chạy stress test"""
        self.start_time = time.time()
        self.is_running = True
        logger.info(f"Starting stress test with {self.total_clients} clients over {self.ramp_up_time} seconds")
        
        # Tính thời gian chờ giữa các kết nối mới
        delay_between_clients = self.ramp_up_time / self.total_clients if self.total_clients > 0 else 0
        
        # Tạo và kết nối các clients
        client_tasks = []
        for i in range(self.total_clients):
            # Tạo ID duy nhất cho mỗi kết nối
            connection_id = str(uuid.uuid4())
            
            # Khởi tạo client
            client = WHEPClient(
                whep_url=self.whep_url,
                connection_id=connection_id,
                ice_servers=self.ice_servers,
                record_path=self.record_path,
                stats_interval=self.stats_interval
            )
            
            # Lưu client vào từ điển
            self.clients[connection_id] = client
            
            # Tạo task để kết nối client
            task = asyncio.create_task(self._connect_client(client, i * delay_between_clients))
            client_tasks.append(task)
            
        # Đợi tất cả các clients kết nối
        await asyncio.gather(*client_tasks)
        
        # Chạy test trong khoảng thời gian xác định
        logger.info(f"All clients connected. Running test for {self.test_duration} seconds")
        await asyncio.sleep(self.test_duration)
        
        # Kết thúc test
        await self.stop()
        
    async def _connect_client(self, client: WHEPClient, delay: float):
        """Kết nối một client sau một khoảng thời gian delay"""
        if delay > 0:
            await asyncio.sleep(delay)
            
        logger.info(f"Connecting client {client.connection_id} ({list(self.clients.keys()).index(client.connection_id) + 1}/{self.total_clients})")
        await client.connect()
        
    async def stop(self):
        """Dừng stress test và thu thập kết quả"""
        if not self.is_running:
            return
            
        self.is_running = False
        logger.info("Stopping stress test and collecting final stats")
        
        # Đóng tất cả các kết nối
        close_tasks = []
        for client in self.clients.values():
            close_tasks.append(asyncio.create_task(client.close("Test completed")))
            
        await asyncio.gather(*close_tasks)
        
        # Thu thập thống kê
        for client in self.clients.values():
            self.all_stats.append(client.stats)
            
        # Tính toán và hiển thị kết quả
        self._calculate_results()
        
    def _calculate_results(self):
        """Tính toán và hiển thị kết quả stress test"""
        logger.info("=== Stress Test Results ===")
        
        # Tính tổng thời gian test
        total_time = time.time() - self.start_time
        logger.info(f"Total test duration: {total_time:.2f} seconds")
        
        # Tính số lượng kết nối thành công và thất bại
        successful_connections = len([s for s in self.all_stats if s.ice_connection_time is not None])
        failed_connections = len(self.all_stats) - successful_connections
        
        logger.info(f"Total clients: {len(self.all_stats)}")
        logger.info(f"Successful connections: {successful_connections} ({successful_connections / len(self.all_stats) * 100:.1f}%)")
        logger.info(f"Failed connections: {failed_connections} ({failed_connections / len(self.all_stats) * 100:.1f}%)")
        
        # Tính thời gian thiết lập trung bình
        setup_times = [s.setup_time for s in self.all_stats if s.setup_time is not None]
        if setup_times:
            avg_setup_time = sum(setup_times) / len(setup_times)
            min_setup_time = min(setup_times)
            max_setup_time = max(setup_times)
            logger.info(f"Average SDP negotiation time: {avg_setup_time:.2f} seconds")
            logger.info(f"Min SDP negotiation time: {min_setup_time:.2f} seconds")
            logger.info(f"Max SDP negotiation time: {max_setup_time:.2f} seconds")
        
        # Tính thời gian ICE kết nối trung bình
        ice_times = [s.ice_connection_time for s in self.all_stats if s.ice_connection_time is not None]
        if ice_times:
            avg_ice_time = sum(ice_times) / len(ice_times)
            min_ice_time = min(ice_times)
            max_ice_time = max(ice_times)
            logger.info(f"Average ICE connection time: {avg_ice_time:.2f} seconds")
            logger.info(f"Min ICE connection time: {min_ice_time:.2f} seconds")
            logger.info(f"Max ICE connection time: {max_ice_time:.2f} seconds")
        
        # Tính tổng dữ liệu đã nhận
        total_bytes = sum(s.bytes_received for s in self.all_stats)
        logger.info(f"Total data received: {total_bytes / 1024 / 1024:.2f} MB")
        
        # Tính tổng số gói tin đã nhận và bị mất
        total_packets = sum(s.packets_received for s in self.all_stats)
        total_lost = sum(s.packets_lost for s in self.all_stats)
        if total_packets + total_lost > 0:
            packet_loss_rate = total_lost / (total_packets + total_lost) * 100
            logger.info(f"Total packets received: {total_packets}")
            logger.info(f"Total packets lost: {total_lost}")
            logger.info(f"Packet loss rate: {packet_loss_rate:.2f}%")
        
        # Tính jitter và RTT trung bình
        jitters = [s.jitter for s in self.all_stats if s.jitter > 0]
        if jitters:
            avg_jitter = sum(jitters) / len(jitters)
            logger.info(f"Average jitter: {avg_jitter * 1000:.2f} ms")
        
        rtts = [s.rtt for s in self.all_stats if s.rtt > 0]
        if rtts:
            avg_rtt = sum(rtts) / len(rtts)
            logger.info(f"Average RTT: {avg_rtt * 1000:.2f} ms")
        
        # Ghi kết quả ra file
        self._save_results_to_file()
        
        # Tạo biểu đồ kết quả
        self._generate_charts()
        
    def _save_results_to_file(self):
        """Lưu kết quả stress test vào file JSON"""
        results = {
            "test_config": {
                "whep_url": self.whep_url,
                "total_clients": self.total_clients,
                "ramp_up_time": self.ramp_up_time,
                "test_duration": self.test_duration,
            },
            "results": {
                "total_time": time.time() - self.start_time,
                "successful_connections": len([s for s in self.all_stats if s.ice_connection_time is not None]),
                "failed_connections": len([s for s in self.all_stats if s.ice_connection_time is None]),
                "client_stats": [
                    {
                        "connection_id": s.connection_id,
                        "start_time": s.start_time,
                        "setup_time": s.setup_time,
                        "ice_connection_time": s.ice_connection_time,
                        "final_state": s.connection_state,
                        "bytes_received": s.bytes_received,
                        "packets_received": s.packets_received,
                        "packets_lost": s.packets_lost,
                        "jitter": s.jitter,
                        "rtt": s.rtt,
                        "error": s.error
                    }
                    for s in self.all_stats
                ]
            }
        }
        
        with open("stress_test_results.json", "w") as f:
            json.dump(results, f, indent=2)
            
        logger.info("Results saved to stress_test_results.json")
        
    def _generate_charts(self):
        """Tạo biểu đồ kết quả stress test"""
        try:
            # Tạo figure với 2x2 subplots
            fig, axs = plt.subplots(2, 2, figsize=(15, 10))
            
            # 1. Biểu đồ thành công/thất bại
            successful = len([s for s in self.all_stats if s.ice_connection_time is not None])
            failed = len(self.all_stats) - successful
            axs[0, 0].pie(
                [successful, failed], 
                labels=["Success", "Failed"], 
                autopct='%1.1f%%',
                colors=['#4CAF50', '#F44336']
            )
            axs[0, 0].set_title('Connection Success Rate')
            
            # 2. Biểu đồ thời gian thiết lập kết nối
            setup_times = [(s.connection_id, s.setup_time) for s in self.all_stats if s.setup_time is not None]
            setup_times.sort(key=lambda x: x[1])
            
            if setup_times:
                connection_ids = [x[0][-6:] for x in setup_times]  # Lấy 6 ký tự cuối của ID
                times = [x[1] for x in setup_times]
                
                axs[0, 1].bar(range(len(times)), times, color='#2196F3')
                axs[0, 1].set_title('SDP Negotiation Time (seconds)')
                axs[0, 1].set_xlabel('Client ID (last 6 chars)')
                axs[0, 1].set_ylabel('Time (s)')
                # Hiển thị nhãn cho trục x nếu có ít hơn 20 kết nối
                if len(connection_ids) <= 20:
                    axs[0, 1].set_xticks(range(len(connection_ids)))
                    axs[0, 1].set_xticklabels(connection_ids, rotation=45)
                else:
                    # Chỉ hiển thị một số nhãn nếu có quá nhiều
                    indices = np.linspace(0, len(connection_ids)-1, 10, dtype=int)
                    axs[0, 1].set_xticks(indices)
                    axs[0, 1].set_xticklabels([connection_ids[i] for i in indices], rotation=45)
            
            # 3. Biểu đồ thời gian kết nối ICE
            ice_times = [(s.connection_id, s.ice_connection_time) for s in self.all_stats if s.ice_connection_time is not None]
            ice_times.sort(key=lambda x: x[1])
            
            if ice_times:
                connection_ids = [x[0][-6:] for x in ice_times]
                times = [x[1] for x in ice_times]
                
                axs[1, 0].bar(range(len(times)), times, color='#FF9800')
                axs[1, 0].set_title('ICE Connection Time (seconds)')
                axs[1, 0].set_xlabel('Client ID (last 6 chars)')
                axs[1, 0].set_ylabel('Time (s)')
                # Hiển thị nhãn cho trục x nếu có ít hơn 20 kết nối
                if len(connection_ids) <= 20:
                    axs[1, 0].set_xticks(range(len(connection_ids)))
                    axs[1, 0].set_xticklabels(connection_ids, rotation=45)
                else:
                    # Chỉ hiển thị một số nhãn nếu có quá nhiều
                    indices = np.linspace(0, len(connection_ids)-1, 10, dtype=int)
                    axs[1, 0].set_xticks(indices)
                    axs[1, 0].set_xticklabels([connection_ids[i] for i in indices], rotation=45)
            
            # 4. Biểu đồ tỷ lệ mất gói tin
            clients_with_packets = [(s.connection_id, s.packets_received, s.packets_lost) 
                                     for s in self.all_stats 
                                     if s.packets_received > 0]
            clients_with_packets.sort(key=lambda x: x[2] / (x[1] + x[2]) if x[1] + x[2] > 0 else 0)
            
            if clients_with_packets:
                connection_ids = [x[0][-6:] for x in clients_with_packets]
                loss_rates = [x[2] / (x[1] + x[2]) * 100 if x[1] + x[2] > 0 else 0 for x in clients_with_packets]
                
                axs[1, 1].bar(range(len(loss_rates)), loss_rates, color='#9C27B0')
                axs[1, 1].set_title('Packet Loss Rate (%)')
                axs[1, 1].set_xlabel('Client ID (last 6 chars)')
                axs[1, 1].set_ylabel('Loss Rate (%)')
                axs[1, 1].set_ylim(0, max(loss_rates) * 1.1 if loss_rates else 1)
                # Hiển thị nhãn cho trục x nếu có ít hơn 20 kết nối
                if len(connection_ids) <= 20:
                    axs[1, 1].set_xticks(range(len(connection_ids)))
                    axs[1, 1].set_xticklabels(connection_ids, rotation=45)
                else:
                    # Chỉ hiển thị một số nhãn nếu có quá nhiều
                    indices = np.linspace(0, len(connection_ids)-1, 10, dtype=int)
                    axs[1, 1].set_xticks(indices)
                    axs[1, 1].set_xticklabels([connection_ids[i] for i in indices], rotation=45)
            
            plt.tight_layout()
            plt.savefig("stress_test_results.png")
            logger.info("Charts saved to stress_test_results.png")
            
        except Exception as e:
            logger.error(f"Failed to generate charts: {str(e)}")

async def main():
    # Phân tích tham số dòng lệnh
    parser = argparse.ArgumentParser(description="WebRTC WHEP Stress Test Tool")
    parser.add_argument("--url", "-u", required=True, help="WHEP endpoint URL")
    parser.add_argument("--clients", "-c", type=int, default=10, help="Number of simultaneous clients")
    parser.add_argument("--ramp-up", "-r", type=int, default=30, help="Ramp-up time in seconds")
    parser.add_argument("--duration", "-d", type=int, default=60, help="Test duration in seconds")
    parser.add_argument("--ice-servers", "-i", type=str, help="JSON string with ICE servers")
    parser.add_argument("--record", "-o", type=str, help="Path to save recordings")
    parser.add_argument("--stats-interval", "-s", type=int, default=5, help="Stats collection interval in seconds")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    
    args = parser.parse_args()
    
    # Thiết lập mức độ log dựa trên verbose flag
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    # Phân tích các ICE servers từ tham số
    ice_servers = None
    if args.ice_servers:
        try:
            ice_servers = json.loads(args.ice_servers)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse ICE servers: {str(e)}")
            return
    
    # Khởi tạo stress test
    stress_test = StressTest(
        whep_url=args.url,
        total_clients=args.clients,
        ramp_up_time=args.ramp_up,
        test_duration=args.duration,
        ice_servers=ice_servers,
        record_path=args.record,
        stats_interval=args.stats_interval
    )
    
    # Chạy stress test
    try:
        await stress_test.run()
    except KeyboardInterrupt:
        logger.info("Test interrupted by user")
        await stress_test.stop()
    except Exception as e:
        logger.error(f"Test failed: {str(e)}")
        await stress_test.stop()

if __name__ == "__main__":
    # Chạy event loop
    asyncio.run(main())