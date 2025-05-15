import asyncio, re
from aiortc import RTCPeerConnection, RTCIceServer, RTCSessionDescription, RTCConfiguration, RTCInboundRtpStreamStats
import aiohttp
import matplotlib.pyplot as plt

WHEP_URL = "http://<ĐỊA_CHỈ_SERVER>:8889/<TÊN_STREAM>/whep"  # Cập nhật URL WHEP của bạn
NUM_CLIENTS = int(input("Nhập số lượng client giả lập: "))
TEST_DURATION = 30  # thời gian duy trì kết nối (giây)

# Danh sách lưu trữ số liệu trung bình theo thời gian
time_points = []
jitter_series = []
loss_series = []

async def run_client(index):
    """Khởi tạo một kết nối WebRTC (WHEP) cho client giả lập có chỉ số index."""
    async with aiohttp.ClientSession() as session:
        # Bước 1: Lấy ICE servers từ server qua WHEP (HTTP OPTIONS)
        ice_servers = []
        async with session.options(WHEP_URL) as res:
            # Trích xuất các header "Link" chứa thông tin ICE server (nếu có)
            for k, v in res.headers.items():
                if k.lower() == "link":
                    m = re.match(r'^<(?P<url>.+?)>; rel="ice-server"(; username="(?P<user>.*?)"; credential="(?P<cred>.*?)"; credential-type="password")?', v)
                    if m:
                        ice_url = m.group("url"); user = m.group("user"); cred = m.group("cred")
                        ice_servers.append(RTCIceServer(urls=ice_url, username=user, credential=cred))
        # Cấu hình kết nối với ICE servers
        pc = RTCPeerConnection(RTCConfiguration(ice_servers))
        # Thêm transceivers để nhận audio/video
        pc.addTransceiver("video", direction="recvonly")
        pc.addTransceiver("audio", direction="recvonly")

        # Callback: in trạng thái kết nối (optional)
        @pc.on("connectionstatechange")
        async def on_state_change():
            print(f"Client {index}: Connection state = {pc.connectionState}")

        # Callback: Xử lý track nhận được (tiêu thụ frame để không đầy bộ đệm)
        @pc.on("track")
        async def on_track(track):
            print(f"Client {index}: nhận track {track.kind}")
            # Chạy vòng lặp nhận frame cho đến khi track kết thúc
            while True:
                try:
                    frame = await track.recv()
                except Exception:
                    break  # ngắt nếu track kết thúc hoặc lỗi

        # Bước 2: Tạo offer SDP và gửi tới server qua WHEP
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        async with session.post(WHEP_URL, headers={"Content-Type": "application/sdp"}, data=offer.sdp) as resp:
            answer_sdp = await resp.text()
        # Thiết lập answer từ server
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))

        # Bước 3: Duy trì kết nối trong TEST_DURATION, định kỳ thu thập số liệu
        start = asyncio.get_running_loop().time()
        while True:
            await asyncio.sleep(5)  # đợi 5s
            elapsed = asyncio.get_running_loop().time() - start
            # Lấy thống kê từ PeerConnection
            stats = await pc.getStats()
            # Duyệt các stats tìm inbound RTP (video + audio)
            total_jitter = 0.0; stream_count = 0; total_lost = 0
            for stat in stats.values():
                if isinstance(stat, RTCInboundRtpStreamStats):
                    total_lost += stat.packetsLost
                    # Jitter đo bằng giây, đổi sang ms:
                    total_jitter += stat.jitter * 1000  
                    stream_count += 1
            if stream_count > 0:
                avg_jitter = total_jitter / stream_count
            else:
                avg_jitter = 0.0
            print(f"[Client {index}] Time {int(elapsed)}s: Jitter = {avg_jitter:.1f} ms, Packets Lost = {total_lost}")
            # Lưu số liệu cho biểu đồ (chỉ client 0 để đại diện, hoặc có thể thu thập tất cả)
            if index == 0:
                time_points.append(elapsed)
                jitter_series.append(avg_jitter)
                loss_series.append(total_lost)
            # Thoát loop nếu hết thời gian
            if elapsed >= TEST_DURATION:
                break

        # Đóng kết nối WebRTC
        await pc.close()
        
async def main():
    tasks = [asyncio.create_task(run_client(i)) for i in range(NUM_CLIENTS)]
    await asyncio.gather(*tasks)

# Thực thi chương trình
asyncio.run(main())

# Vẽ biểu đồ kết quả (dùng dữ liệu đã thu thập từ client 0)
plt.figure()
plt.plot(time_points, jitter_series, label="Jitter (ms)")
plt.plot(time_points, loss_series, label="Packets Lost", color="orange")
plt.xlabel("Thời gian (s)"); plt.legend()
plt.show()