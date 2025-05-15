package main

import (
	"crypto/tls"
	"flag"
	"fmt"
	"github.com/pion/interceptor"
	"github.com/pion/logging"
	"github.com/pion/rtcp"
	"github.com/pion/webrtc/v3"
	"io"
	"net"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// RegisterSupportedCodecs Copied and re-worked version of func (m *MediaEngine) RegisterDefaultCodecs() error
func RegisterSupportedCodecs(m *webrtc.MediaEngine) error {
	{
		audioRTCPFeedback := []webrtc.RTCPFeedback{{"nack", ""}}

		for _, codec := range []webrtc.RTPCodecParameters{
			{
				RTPCodecCapability: webrtc.RTPCodecCapability{webrtc.MimeTypeOpus, 48000, 2,
					"minptime=10;useinbandfec=1", audioRTCPFeedback},
				PayloadType: 111,
			},
			{
				RTPCodecCapability: webrtc.RTPCodecCapability{webrtc.MimeTypeG722, 8000, 0,
					"", audioRTCPFeedback},
				PayloadType: 9,
			},
			{
				RTPCodecCapability: webrtc.RTPCodecCapability{webrtc.MimeTypePCMU, 8000, 0,
					"", audioRTCPFeedback},
				PayloadType: 0,
			},
			{
				RTPCodecCapability: webrtc.RTPCodecCapability{webrtc.MimeTypePCMA, 8000, 0,
					"", audioRTCPFeedback},
				PayloadType: 8,
			},
		} {
			if err := m.RegisterCodec(codec, webrtc.RTPCodecTypeAudio); err != nil {
				return err
			}
		}
	}

	videoRTCPFeedback := []webrtc.RTCPFeedback{{"goog-remb", ""}, {"ccm", "fir"},
		{"nack", ""}, {"nack", "pli"}}

	for _, codec := range []webrtc.RTPCodecParameters{
		{
			RTPCodecCapability: webrtc.RTPCodecCapability{webrtc.MimeTypeVP8, 90000, 0,
				"", videoRTCPFeedback},
			PayloadType: 96,
		},
		{
			RTPCodecCapability: webrtc.RTPCodecCapability{webrtc.MimeTypeH264, 90000, 0,
				"level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f", videoRTCPFeedback},
			PayloadType: 102,
		},
		{
			RTPCodecCapability: webrtc.RTPCodecCapability{webrtc.MimeTypeH264, 90000, 0,
				"level-asymmetry-allowed=1;packetization-mode=0;profile-level-id=42001f",
				videoRTCPFeedback},
			PayloadType: 104,
		},
		{
			RTPCodecCapability: webrtc.RTPCodecCapability{webrtc.MimeTypeH264, 90000, 0,
				"level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f", videoRTCPFeedback},
			PayloadType: 106,
		},
		{
			RTPCodecCapability: webrtc.RTPCodecCapability{webrtc.MimeTypeH264, 90000, 0,
				"level-asymmetry-allowed=1;packetization-mode=0;profile-level-id=42e01f", videoRTCPFeedback},
			PayloadType: 108,
		},
		{
			RTPCodecCapability: webrtc.RTPCodecCapability{webrtc.MimeTypeH264, 90000, 0,
				"level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=4d001f", videoRTCPFeedback},
			PayloadType: 127,
		},
		{
			RTPCodecCapability: webrtc.RTPCodecCapability{webrtc.MimeTypeH264, 90000, 0,
				"level-asymmetry-allowed=1;packetization-mode=0;profile-level-id=4d001f", videoRTCPFeedback},
			PayloadType: 39,
		},
		{
			RTPCodecCapability: webrtc.RTPCodecCapability{webrtc.MimeTypeAV1, 90000, 0,
				"", videoRTCPFeedback},
			PayloadType: 45,
		},
		{
			RTPCodecCapability: webrtc.RTPCodecCapability{webrtc.MimeTypeVP9, 90000, 0,
				"profile-id=0", videoRTCPFeedback},
			PayloadType: 98,
		},
		{
			RTPCodecCapability: webrtc.RTPCodecCapability{webrtc.MimeTypeVP9, 90000, 0,
				"profile-id=2", videoRTCPFeedback},
			PayloadType: 100,
		},
		{
			RTPCodecCapability: webrtc.RTPCodecCapability{webrtc.MimeTypeH264, 90000, 0,
				"level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=64001f", videoRTCPFeedback},
			PayloadType: 112,
		},
	} {
		if err := m.RegisterCodec(codec, webrtc.RTPCodecTypeVideo); err != nil {
			return err
		}
	}

	return nil
}

// DevNullWriter Ignore all input logs
type DevNullWriter struct {
}

// implements io.Writer interface
func (writer DevNullWriter) Write(p []byte) (n int, err error) { return n, nil }

// VerboseWriter logs to stdout
type VerboseWriter struct {
}

// implements io.Writer interface
func (writer VerboseWriter) Write(p []byte) (n int, err error) {
	fmt.Print(string(p))
	return len(p), nil
}

type NackStatsFactory struct {
	lost atomic.Uint64
}

func (r *NackStatsFactory) updateLostPackets(u uint64) {
	r.lost.Add(u)
}

func (r *NackStatsFactory) getLost() uint64 {
	return r.lost.Swap(0)
}

type NackStatsInterceptor struct {
	factory *NackStatsFactory
	interceptor.NoOp
}

// NewInterceptor constructs a new ResponderInterceptor
func (r *NackStatsFactory) NewInterceptor(_ string) (interceptor.Interceptor, error) {
	i := &NackStatsInterceptor{factory: r}
	return i, nil
}

// BindRTCPWriter lets you modify any outgoing RTCP packets. It is called once per PeerConnection. The returned method
// will be called once per packet batch.
func (n *NackStatsInterceptor) BindRTCPWriter(writer interceptor.RTCPWriter) interceptor.RTCPWriter {
	return interceptor.RTCPWriterFunc(func(pkts []rtcp.Packet, attributes interceptor.Attributes) (int, error) {
		for _, p := range pkts {
			report, ok := p.(*rtcp.TransportLayerNack)
			if report == nil || !ok {
				break
			}
			lostPackets := 0

			for _, nack := range report.Nacks {
				lostPackets += len(nack.PacketList())
			}
			n.factory.updateLostPackets(uint64(lostPackets))
		}
		return writer.Write(pkts, attributes)
	})
}

func CreateWebRtcApi(factory *NackStatsFactory, verbose bool) *webrtc.API {
	m := &webrtc.MediaEngine{}

	if RegisterSupportedCodecs(m) != nil {
		return nil
	}

	i := &interceptor.Registry{}

	i.Add(factory)
	if err := webrtc.RegisterDefaultInterceptors(m, i); err != nil {
		return nil
	}

	if err := RegisterSupportedCodecs(m); err != nil {
		return nil
	}

	s := webrtc.SettingEngine{}

	logFactory := &logging.DefaultLoggerFactory{}
	
	// Sử dụng logger nếu mode verbose được bật
	if verbose {
		logFactory.Writer = &VerboseWriter{}
	} else {
		logFactory.Writer = &DevNullWriter{}
	}
	
	s.LoggerFactory = logFactory

	_ = s.SetAnsweringDTLSRole(webrtc.DTLSRoleClient)

	// Cấu hình ICE timeouts
	s.SetICETimeouts(
		5*time.Second,  // ICE disconnected timeout
		30*time.Second, // ICE failed timeout
		5*time.Second,  // ICE keepalive interval
	)

	// Cho phép sử dụng các ứng cử viên loopback
	s.SetIncludeLoopbackCandidate(true)
	
	// Bật tính năng ICE trickle để tối ưu thời gian kết nối

	return webrtc.NewAPI(webrtc.WithMediaEngine(m), webrtc.WithInterceptorRegistry(i),
		webrtc.WithSettingEngine(s))
}

func webrtcSession(api *webrtc.API, whepUrl string, session *atomic.Int64,
	repliedSessions *atomic.Int64, activeWebRTCSessions *atomic.Int64, 
	traffic *atomic.Uint64, iceServers []webrtc.ICEServer, verbose bool) {
	
	defer func() {
		session.Add(-1)
	}()

	// Tạo cấu hình với ICE servers
	config := webrtc.Configuration{
		ICEServers: iceServers,
	}

	// Create new PeerConnection
	peerConnection, err := api.NewPeerConnection(config)

	if err != nil {
		fmt.Printf("Error creating PeerConnection: %v\n", err)
		return
	}

	// When this frame returns close the PeerConnection
	defer peerConnection.Close() //nolint

	// Thêm log trạng thái ICE nếu ở chế độ verbose
	if verbose {
		peerConnection.OnICEGatheringStateChange(func(is webrtc.ICEGathererState) {
			fmt.Printf("ICE gathering state changed to: %s\n", is.String())
		})
		
		peerConnection.OnICEConnectionStateChange(func(connectionState webrtc.ICEConnectionState) {
			fmt.Printf("ICE Connection State has changed: %s\n", connectionState.String())
		})

		peerConnection.OnICECandidate(func(candidate *webrtc.ICECandidate) {
			if candidate != nil {
				fmt.Printf("ICE candidate: %s\n", candidate.String())
			}
		})
	}

	if _, err := peerConnection.AddTransceiverFromKind(webrtc.RTPCodecTypeVideo, webrtc.RTPTransceiverInit{
		Direction: webrtc.RTPTransceiverDirectionRecvonly}); err != nil {
		fmt.Printf("Error adding video transceiver: %v\n", err)
		return
	}

	if _, err := peerConnection.AddTransceiverFromKind(webrtc.RTPCodecTypeAudio, webrtc.RTPTransceiverInit{
		Direction: webrtc.RTPTransceiverDirectionRecvonly}); err != nil {
		fmt.Printf("Error adding audio transceiver: %v\n", err)
		return
	}

	desc, err := peerConnection.CreateOffer(nil)
	if err != nil {
		fmt.Printf("Error creating offer: %v\n", err)
		return
	}

	if err = peerConnection.SetLocalDescription(desc); err != nil {
		fmt.Printf("Error setting local description: %v\n", err)
		return
	}

	// Chờ hoàn thành ICE gathering trước khi gửi offer
	gatherComplete := webrtc.GatheringCompletePromise(peerConnection)
	
	// Đợi gathering hoàn tất với timeout
	select {
	case <-gatherComplete:
		if verbose {
			fmt.Println("ICE gathering completed")
		}
	case <-time.After(10 * time.Second):
		if verbose {
			fmt.Println("ICE gathering timed out, continuing with available candidates")
		}
	}

	// Lấy mô tả cuối cùng với các ICE candidates
	updatedDesc := peerConnection.LocalDescription()

	context := net.Dialer{
		Timeout: 10 * time.Second, // Tăng timeout lên để đợi kết nối qua TURN
	}

	tripper := &http.Transport{
		Proxy:               http.ProxyFromEnvironment,
		DialContext:         context.DialContext,
		ForceAttemptHTTP2:   true,
		MaxIdleConns:        1,
		IdleConnTimeout:     10 * time.Second,
		TLSHandshakeTimeout: 10 * time.Second,
		TLSClientConfig:     &tls.Config{InsecureSkipVerify: true},
	}
	
	client := http.Client{
		Transport: tripper,
		Timeout:   20 * time.Second, // Tăng timeout lên để đợi kết nối qua TURN
	}
	
	// Gửi SDP offer đã hoàn thiện
	resp, err := client.Post(whepUrl, "application/sdp", strings.NewReader(updatedDesc.SDP))

	if err != nil {
		fmt.Printf("HTTP request error: %v\n", err)
		return
	}

	if resp == nil {
		fmt.Printf("Was not able to connect to %s\n", whepUrl)
		return
	}

	defer resp.Body.Close()

	if resp.StatusCode != 201 {
		fmt.Printf("Was not able to connect to %s (status code: %d)\n", whepUrl, resp.StatusCode)
		bodyBytes, _ := io.ReadAll(resp.Body)
		if len(bodyBytes) > 0 {
			fmt.Printf("Response body: %s\n", string(bodyBytes))
		}
		return
	}

	sdpBytes, err := io.ReadAll(resp.Body)

	if err != nil || len(sdpBytes) == 0 {
		fmt.Printf("Was not able read sdp %s: %v\n", whepUrl, err)
		return
	}

	repliedSessions.Add(1)

	if err = peerConnection.SetRemoteDescription(webrtc.SessionDescription{
		Type: webrtc.SDPTypeAnswer, 
		SDP: string(sdpBytes),
	}); err != nil {
		fmt.Printf("Error setting remote description: %v\n", err)
		return
	}

	sessionChannel := make(chan bool)

	doneMutex := sync.Mutex{}
	done := false

	// If PeerConnection is closed remove it from global list
	peerConnection.OnConnectionStateChange(func(p webrtc.PeerConnectionState) {
		if verbose {
			fmt.Printf("Connection state changed to: %s\n", p.String())
		}
		
		switch p {
		case webrtc.PeerConnectionStateDisconnected:
			_ = peerConnection.Close()
		case webrtc.PeerConnectionStateFailed:
			_ = peerConnection.Close()
		case webrtc.PeerConnectionStateClosed:
			doneMutex.Lock()
			done = true
			doneMutex.Unlock()
			sessionChannel <- true
		case webrtc.PeerConnectionStateConnected:
			activeWebRTCSessions.Add(1)
		}
	})

	peerConnection.OnTrack(func(t *webrtc.TrackRemote, r *webrtc.RTPReceiver) {
		if verbose {
			fmt.Printf("Got track: %s\n", t.ID())
		}
		
		for {
			p, _, err := t.ReadRTP()

			if err != nil {
				doneMutex.Lock()
				local := done
				doneMutex.Unlock()

				if local || err == io.EOF {
					return
				} else {
					continue
				}
			} else {
				val := p.MarshalSize()
				traffic.Add(uint64(val))
			}
		}
	})

	<-sessionChannel
}

func main() {
	whepUrl := flag.String("whep-addr", "", "whep live stream url")
	whepSessionsCount := flag.Int("whep-sessions", 1, "whep sessions count")
	
	// Thêm các tham số cho TURN server
	turnServerURL := flag.String("turn-url", "", "TURN server URL (e.g. turn:turn.example.com:3478)")
	turnUsername := flag.String("turn-username", "", "TURN server username")
	turnPassword := flag.String("turn-password", "", "TURN server password")
	verboseMode := flag.Bool("verbose", false, "Enable verbose logging")
	
	// Thêm STUN server
	stunServer := flag.String("stun-url", "stun:stun.l.google.com:19302", "STUN server URL")
	
	flag.Parse()

	if whepUrl == nil || len(*whepUrl) == 0 || whepSessionsCount == nil || *whepSessionsCount <= 0 {
		fmt.Println("wrong params specified")
		return
	}

	// Tạo cấu hình ICE servers
	var iceServers []webrtc.ICEServer
	
	// Thêm STUN server
	if stunServer != nil && len(*stunServer) > 0 {
		iceServers = append(iceServers, webrtc.ICEServer{
			URLs: []string{*stunServer},
		})
	}
	
	// Thêm TURN server nếu được chỉ định
	if turnServerURL != nil && len(*turnServerURL) > 0 {
		turnServer := webrtc.ICEServer{
			URLs: []string{*turnServerURL},
		}
		
		if turnUsername != nil && turnPassword != nil && 
		   len(*turnUsername) > 0 && len(*turnPassword) > 0 {
			turnServer.Username = *turnUsername
			turnServer.Credential = *turnPassword
			turnServer.CredentialType = webrtc.ICECredentialTypePassword
		}
		
		iceServers = append(iceServers, turnServer)
	}
	
	if len(iceServers) > 0 && *verboseMode {
		fmt.Printf("Using ICE servers: %+v\n", iceServers)
	}

	var sessions atomic.Int64
	sessions.Store(int64(*whepSessionsCount))

	var repliedSessions atomic.Int64
	repliedSessions.Store(0)

	var activeWebRTCSessions atomic.Int64
	activeWebRTCSessions.Store(0)

	factory := NackStatsFactory{}
	api := CreateWebRtcApi(&factory, *verboseMode)

	var traffic atomic.Uint64
	traffic.Store(0)

	for i := 0; i < *whepSessionsCount; i++ {
		go webrtcSession(api, *whepUrl, &sessions, &repliedSessions, 
		                 &activeWebRTCSessions, &traffic, iceServers, *verboseMode)
	}

	lastBitrateReport := time.Now()

	ticker := time.NewTicker(time.Second * 1)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			if sessions.Load() == 0 {
				return
			}
			now := time.Now()
			interval := now.Sub(lastBitrateReport)
			lastBitrateReport = now
			bandwidthMb := (float64(traffic.Swap(0) * 8 * 1000)) / (float64(interval.Milliseconds()) * 1000 * 1000)
			fmt.Printf("Sessions=%d Confirmed Session=%d Active WebRTC Sessions=%d Bandwidth(Mbit/s)= %.4f Packet Loss=%d\n", 
				sessions.Load(), repliedSessions.Load(), activeWebRTCSessions.Load(), 
				bandwidthMb, factory.getLost())
		}
	}
}