"""
SDN Bandwidth Measurement Project - Custom Topology
====================================================
Creates multiple topologies for bandwidth comparison:
  - Single Switch (baseline)
  - Linear (chain of switches)
  - Tree (hierarchical)

All topologies use TCLink for bandwidth/delay constraints.
"""

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.cli import CLI
from mininet.node import RemoteController


# ─────────────────────────────────────────────
# Topology 1: Single Switch (Baseline)
# ─────────────────────────────────────────────
class SingleSwitchTopo(Topo):
    """
    4 hosts connected to 1 switch.
    Baseline for maximum achievable bandwidth.

        h1 ─┐
        h2 ─┤── s1
        h3 ─┤
        h4 ─┘
    """
    def build(self, bw=10, delay='2ms'):
        s1 = self.addSwitch('s1')
        for i in range(1, 5):
            h = self.addHost(f'h{i}', ip=f'10.0.0.{i}/24')
            self.addLink(h, s1, bw=bw, delay=delay, loss=0)


# ─────────────────────────────────────────────
# Topology 2: Linear (Chain)
# ─────────────────────────────────────────────
class LinearTopo(Topo):
    """
    4 hosts connected via 3 switches in a chain.
    Each hop adds delay/bottleneck.

        h1 ── s1 ── s2 ── s3 ── h4
               |          |
              h2          h3
    """
    def build(self, bw=10, delay='5ms'):
        switches = [self.addSwitch(f's{i}') for i in range(1, 4)]

        # Link switches in a chain
        for i in range(len(switches) - 1):
            self.addLink(switches[i], switches[i + 1], bw=bw, delay=delay)

        # Attach hosts: h1→s1, h2→s1, h3→s3, h4→s3
        h1 = self.addHost('h1', ip='10.0.0.1/24')
        h2 = self.addHost('h2', ip='10.0.0.2/24')
        h3 = self.addHost('h3', ip='10.0.0.3/24')
        h4 = self.addHost('h4', ip='10.0.0.4/24')

        self.addLink(h1, switches[0], bw=bw, delay=delay)
        self.addLink(h2, switches[0], bw=bw, delay=delay)
        self.addLink(h3, switches[2], bw=bw, delay=delay)
        self.addLink(h4, switches[2], bw=bw, delay=delay)


# ─────────────────────────────────────────────
# Topology 3: Tree (Hierarchical)
# ─────────────────────────────────────────────
class TreeTopo(Topo):
    """
    Tree topology: 1 core switch → 2 aggregation switches → 4 hosts.
    Models a data-center-like hierarchy.

              s1 (core)
             /         \\
           s2           s3
          /  \\         /  \\
         h1   h2      h3   h4
    """
    def build(self, bw=10, delay='3ms'):
        core = self.addSwitch('s1')
        agg1 = self.addSwitch('s2')
        agg2 = self.addSwitch('s3')

        # Core ↔ aggregation links (higher BW)
        self.addLink(core, agg1, bw=bw * 2, delay=delay)
        self.addLink(core, agg2, bw=bw * 2, delay=delay)

        # Host links
        for idx, (agg, num) in enumerate([(agg1, [1, 2]), (agg2, [3, 4])]):
            for n in num:
                h = self.addHost(f'h{n}', ip=f'10.0.0.{n}/24')
                self.addLink(h, agg, bw=bw, delay=delay)


# ─────────────────────────────────────────────
# Runner: launch a chosen topology with Ryu
# ─────────────────────────────────────────────
def run(topo_name='single'):
    setLogLevel('info')

    topo_map = {
        'single': SingleSwitchTopo(),
        'linear': LinearTopo(),
        'tree':   TreeTopo(),
    }

    topo = topo_map.get(topo_name, SingleSwitchTopo())

    net = Mininet(
        topo=topo,
        link=TCLink,
        controller=RemoteController('c0', ip='127.0.0.1', port=6633)
    )

    net.start()
    print(f"\n[+] Topology '{topo_name}' started. Entering CLI...\n")
    CLI(net)
    net.stop()


if __name__ == '__main__':
    import sys
    topo_arg = sys.argv[1] if len(sys.argv) > 1 else 'single'
    run(topo_arg)
