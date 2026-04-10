"""
SDN Bandwidth Measurement Project - Ryu Controller
===================================================
Implements:
  - packet_in event handling (learning switch base)
  - Explicit OpenFlow match+action flow rules
  - QoS priority flow rules (prioritize ICMP/ping)
  - Bandwidth monitoring via port statistics
  - Logging of flow table changes and packet counts

Run with:
    ryu-manager controller.py --observe-links
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, icmp, tcp, udp, ether_types
from ryu.lib import hub
import time
import logging

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    filename='controller.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger('BandwidthController')


class BandwidthController(app_manager.RyuApp):
    """
    OpenFlow 1.3 controller for bandwidth measurement and analysis.

    Features
    --------
    1. Learning switch  – MAC table populated on packet_in events.
    2. Explicit flows   – match on dst MAC → output port (no flooding after learning).
    3. QoS flows        – ICMP (ping) gets priority=200, TCP iperf gets priority=100.
    4. Blocking rules   – drop traffic from a configurable blocked IP.
    5. Stats polling    – every 5 s, request port stats and log throughput.
    """

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # ── Tunable knobs ────────────────────────────────────────────────────────
    BLOCKED_IP       = '10.0.0.4'   # h4 is blocked from reaching h1
    STATS_INTERVAL   = 5            # seconds between stats polls
    FLOW_IDLE_TIMEOUT = 30          # idle timeout for learned flows
    FLOW_HARD_TIMEOUT = 120         # hard timeout for learned flows

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # { dpid: { mac: port } }
        self.mac_to_port = {}
        # { dpid: { port_no: (tx_bytes, rx_bytes, timestamp) } }
        self.port_stats  = {}
        # Start background stats-polling thread
        self.monitor_thread = hub.spawn(self._monitor_loop)
        logger.info("BandwidthController started.")

    # ══════════════════════════════════════════════════════════════════════════
    # Helper: send a flow-mod message to the switch
    # ══════════════════════════════════════════════════════════════════════════
    def _add_flow(self, datapath, priority, match, actions,
                  idle_timeout=0, hard_timeout=0, table_id=0):
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(
            ofproto.OFPIT_APPLY_ACTIONS, actions)]

        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=inst,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
            table_id=table_id,
        )
        datapath.send_msg(mod)
        logger.info(
            f"[DPID {datapath.id}] Flow installed | priority={priority} "
            f"match={match} actions={actions}"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Event: Switch connects – install table-miss + QoS + blocking rules
    # ══════════════════════════════════════════════════════════════════════════
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto  = datapath.ofproto
        parser   = datapath.ofproto_parser
        dpid     = datapath.id

        self.mac_to_port.setdefault(dpid, {})
        self.logger.info(f"Switch connected: DPID={dpid}")
        logger.info(f"Switch connected: DPID={dpid}")

        # ── Rule 0: Table-miss → send to controller (priority 0) ──────────
        match   = parser.OFPMatch()
        actions = [parser.OFPActionOutput(
            ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self._add_flow(datapath, 0, match, actions)

        # ── Rule 1: ICMP (ping) – HIGH priority 200 ───────────────────────
        icmp_match = parser.OFPMatch(
            eth_type=ether_types.ETH_TYPE_IP,
            ip_proto=1  # ICMP
        )
        flood_action = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
        self._add_flow(datapath, 200, icmp_match, flood_action,
                       idle_timeout=60)

        # ── Rule 2: TCP (iperf traffic) – MEDIUM priority 100 ─────────────
        tcp_match = parser.OFPMatch(
            eth_type=ether_types.ETH_TYPE_IP,
            ip_proto=6  # TCP
        )
        self._add_flow(datapath, 100, tcp_match, flood_action,
                       idle_timeout=60)

        # ── Rule 3: Block traffic FROM blocked IP (priority 300) ──────────
        # Scenario 2 – blocked vs allowed
        block_match = parser.OFPMatch(
            eth_type=ether_types.ETH_TYPE_IP,
            ipv4_src=self.BLOCKED_IP
        )
        self._add_flow(datapath, 300, block_match, [])   # empty = DROP
        self.logger.info(
            f"[DPID {dpid}] Blocking rule installed: DROP src={self.BLOCKED_IP}"
        )
        logger.info(
            f"[DPID {dpid}] Blocking rule installed: DROP src={self.BLOCKED_IP}"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Event: packet_in – learning switch logic
    # ══════════════════════════════════════════════════════════════════════════
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg      = ev.msg
        datapath = msg.datapath
        ofproto  = datapath.ofproto
        parser   = datapath.ofproto_parser
        dpid     = datapath.id

        # Parse packet
        pkt      = packet.Packet(msg.data)
        eth_pkt  = pkt.get_protocol(ethernet.ethernet)

        if eth_pkt is None:
            return
        if eth_pkt.ethertype == ether_types.ETH_TYPE_LLDP:
            return  # ignore LLDP

        dst = eth_pkt.dst
        src = eth_pkt.src
        in_port = msg.match['in_port']

        # ── Learn MAC → port mapping ──────────────────────────────────────
        self.mac_to_port[dpid][src] = in_port
        self.logger.debug(f"[DPID {dpid}] Learned {src} on port {in_port}")
        logger.info(f"[DPID {dpid}] packet_in src={src} dst={dst} in_port={in_port}")

        # ── Determine output port ─────────────────────────────────────────
        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        # ── Install a specific flow rule once dst is known ────────────────
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(
                in_port=in_port,
                eth_dst=dst,
                eth_src=src
            )
            # Only install if buffer_id is valid (avoids duplicate packets)
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self._add_flow(
                    datapath, 50, match, actions,
                    idle_timeout=self.FLOW_IDLE_TIMEOUT,
                    hard_timeout=self.FLOW_HARD_TIMEOUT
                )
                return   # Switch will forward from buffer
            else:
                self._add_flow(
                    datapath, 50, match, actions,
                    idle_timeout=self.FLOW_IDLE_TIMEOUT,
                    hard_timeout=self.FLOW_HARD_TIMEOUT
                )

        # ── Send packet out ───────────────────────────────────────────────
        data = None if msg.buffer_id != ofproto.OFP_NO_BUFFER else msg.data
        out  = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data
        )
        datapath.send_msg(out)

    # ══════════════════════════════════════════════════════════════════════════
    # Stats monitoring loop (every STATS_INTERVAL seconds)
    # ══════════════════════════════════════════════════════════════════════════
    def _monitor_loop(self):
        """Background thread: request port stats from every known switch."""
        while True:
            for dpid, mac_table in self.mac_to_port.items():
                # We need the datapath object; retrieve via Ryu's internal map
                datapath = self._get_datapath(dpid)
                if datapath:
                    self._request_port_stats(datapath)
            hub.sleep(self.STATS_INTERVAL)

    def _get_datapath(self, dpid):
        """Retrieve a live datapath object by DPID."""
        # RyuApp doesn't expose a direct registry; use the switches dict
        # populated implicitly. We access it via the app's internal dict.
        return self.mac_to_port.get(dpid, {}) and \
               getattr(self, '_datapaths', {}).get(dpid)

    def _request_port_stats(self, datapath):
        """Send OFPPortStatsRequest to the switch."""
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser
        req = parser.OFPPortStatsRequest(
            datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req)

    # ── Register datapath on switch connect ──────────────────────────────────
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _register_datapath(self, ev):
        dp = ev.msg.datapath
        if not hasattr(self, '_datapaths'):
            self._datapaths = {}
        self._datapaths[dp.id] = dp

    # ── Handle port stats reply ───────────────────────────────────────────────
    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now  = time.time()

        self.logger.info(f"\n{'='*55}")
        self.logger.info(f" Port Statistics — DPID {dpid}")
        self.logger.info(f"{'='*55}")
        logger.info(f"Port stats reply — DPID {dpid}")

        for stat in ev.msg.body:
            port = stat.port_no
            tx   = stat.tx_bytes
            rx   = stat.rx_bytes
            tx_p = stat.tx_packets
            rx_p = stat.rx_packets

            # Compute throughput if we have a previous reading
            prev = self.port_stats.get(dpid, {}).get(port)
            if prev:
                dt      = now - prev[2]
                tx_mbps = (tx - prev[0]) * 8 / dt / 1e6
                rx_mbps = (rx - prev[1]) * 8 / dt / 1e6
                self.logger.info(
                    f"  Port {port:>3}: TX={tx_mbps:6.2f} Mbps  "
                    f"RX={rx_mbps:6.2f} Mbps  "
                    f"TX_pkts={tx_p}  RX_pkts={rx_p}"
                )
                logger.info(
                    f"[DPID {dpid}] Port {port}: "
                    f"TX={tx_mbps:.2f}Mbps RX={rx_mbps:.2f}Mbps "
                    f"pkts TX={tx_p} RX={rx_p}"
                )
            else:
                self.logger.info(
                    f"  Port {port:>3}: TX_bytes={tx}  RX_bytes={rx}  "
                    f"TX_pkts={tx_p}  RX_pkts={rx_p}"
                )

            # Store current reading
            self.port_stats.setdefault(dpid, {})[port] = (tx, rx, now)
