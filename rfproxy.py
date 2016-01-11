import struct
import threading

from rflogging import log
from ofinterface import *

import rflib.ipc.IPC as IPC
import rflib.ipc.IPCService as IPCService
from rflib.ipc.RFProtocol import *
from rflib.ipc.RFProtocolFactory import RFProtocolFactory
from rflib.defs import *

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import *
from ryu.topology import switches, event
from ryu.ofproto import ofproto_v1_3 as ofproto
from ryu.lib import hub
from ryu.lib.mac import *
from ryu.lib.dpid import *
from ryu.lib import hub
from ryu.lib.packet.ethernet import ethernet

# Association table
class Table:
    def __init__(self):
        self.dp_to_vs = {}
        self.vs_to_dp = {}

    def update_dp_port(self, dp_id, dp_port, vs_id, vs_port):
                # If there was a mapping for this DP port, reset it
        if (dp_id, dp_port) in self.dp_to_vs:
            old_vs_port = self.dp_to_vs[(dp_id, dp_port)]
            del self.vs_to_dp[old_vs_port]
        self.dp_to_vs[(dp_id, dp_port)] = (vs_id, vs_port)
        self.vs_to_dp[(vs_id, vs_port)] = (dp_id, dp_port)

    def dp_port_to_vs_port(self, dp_id, dp_port):
        try:
            return self.dp_to_vs[(dp_id, dp_port)]
        except KeyError:
            return None

    def vs_port_to_dp_port(self, vs_id, vs_port):
        try:
            return self.vs_to_dp[(vs_id, vs_port)]
        except KeyError:
            return None

    def delete_dp(self, dp_id):
        for (id_, port) in self.dp_to_vs.keys():
            if id_ == dp_id:
                del self.dp_to_vs[(id_, port)]

        for key in self.vs_to_dp.keys():
            id_, port = self.vs_to_dp[key]
            if id_ == dp_id:
                del self.vs_to_dp[key]


class HubThreading(object):
    Thread = staticmethod(threading.Thread)
    Event = staticmethod(hub.Event)
    sleep = staticmethod(hub.sleep)
    name = "HubThreading"


# IPC message Processing
class RFProcessor(IPC.IPCMessageProcessor):

    def __init__(self, switches, table, ipc_send):
        self._switches = switches
        self.table = table
        self.ipc_send = ipc_send

    def send_msg(self, dp, ofmsg):
        while dp.send_q.qsize() > 0:
            hub.sleep(0)
        dp.send_msg(ofmsg)
        hub.sleep(0)
        log.info("ofp_flow_mod %s was sent to datapath (dp_id = %x)",
                 str(ofmsg), dp.id)

    def process(self, from_, to, channel, msg):
        type_ = msg.get_type()
        if type_ == ROUTE_MOD:
            switch = self._switches._get_switch(msg.get_id())
            dp = switch.dp
            ofmsg = None

            if msg.get_mod() in (RMT_ADD, RMT_DELETE, RMT_CONTROLLER):
                ofmsg = create_flow_mod(dp,
                                        msg.get_table(),
                                        msg.get_mod(),
                                        msg.get_matches(),
                                        msg.get_actions(),
                                        msg.get_options())
            elif msg.get_mod() in (RMT_ADD_GROUP, RMT_DELETE_GROUP):
                ofmsg = create_group_mod(dp,
                                         msg.get_mod(),
                                         msg.get_group(),
                                         msg.get_actions())
            elif msg.get_mod() in (RMT_ADD_METER, RMT_DELETE_METER):
                ofmsg = create_meter_mod(dp,
                                         msg.get_mod(),
                                         msg.get_meter(),
                                         msg.get_bands(),
                                         msg.get_flags())
            else:
                log.warning("unknown routemod: %s", msg)
                return

            try:
                self.send_msg(dp, ofmsg)
            except Exception as e:
                log.warning("Error sending ofmsg:")
                log.warning(type(e))
                log.warning(str(e))

            if msg.get_mod() in (RMT_DELETE, RMT_CONTROLLER, RMT_ADD_GROUP, RMT_DELETE_GROUP, RMT_ADD_METER, RMT_DELETE_METER):
                dp.send_barrier()

            self.ipc_send(RFSERVER_RFPROXY_CHANNEL, RFSERVER_ID, msg)

        elif type_ == DATA_PLANE_MAP:
            dp_id = msg.get_dp_id()
            dp_port = msg.get_dp_port()
            vs_id = msg.get_vs_id()
            vs_port = msg.get_vs_port()

            self.table.update_dp_port(dp_id, dp_port, vs_id, vs_port)
            log.info("Updating vs-dp association (vs_id=%s, vs_port=%i, "
                     "dp_id=%s, dp_port=%i" % (dpid_to_str(vs_id), vs_port,
                                               dpid_to_str(dp_id), dp_port))
        else:
            log.info("Got unknown msg type %d", type_)


class RFProxy(app_manager.RyuApp):
    #Listen to the Ryu topology change events
    _CONTEXTS = {'switches': switches.Switches}
    OFP_VERSIONS = [ofproto.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(RFProxy, self).__init__(*args, **kwargs)

        self.ID = 0
        self.table = Table()
        self.switches = kwargs['switches']
        self.ipc = IPCService.for_proxy(str(self.ID), HubThreading)
        self.ipc_lock = threading.Lock()
        self.rfprocess = RFProcessor(self.switches, self.table, self.ipc_send)

        self.ipc.listen(RFSERVER_RFPROXY_CHANNEL, RFProtocolFactory(),
                        self.rfprocess, False)
        log.info("RFProxy running.")

    def ipc_send(self, channel, channel_id, msg):
        self.ipc_lock.acquire()
        self.ipc.send(channel, channel_id, msg)
        self.ipc_lock.release()

    #Event handlers
    @set_ev_cls(event.EventSwitchEnter, MAIN_DISPATCHER)
    def handler_datapath_enter(self, ev):
        dp = ev.switch.dp
        dp_id = dp.id
        log.debug("INFO:rfproxy:Datapath is up (dp_id=%d)", dpid_to_str(dp_id))
        for port in dp.ports:
            if port <= dp.ofproto.OFPP_MAX:
                msg = DatapathPortRegister(ct_id=self.ID, dp_id=dp_id,
                                           dp_port=port)
                self.ipc_send(RFSERVER_RFPROXY_CHANNEL, RFSERVER_ID, msg)
                log.info("Registering datapath port (dp_id=%s, dp_port=%d)",
                         dpid_to_str(dp_id), port)

    @set_ev_cls(event.EventSwitchLeave, MAIN_DISPATCHER)
    def handler_datapath_leave(self, ev):
        dp = ev.switch.dp
        dp_id = dp.id
        log.info("Datapath is down (dp_id=%s)", dpid_to_str(dp_id))
        self.table.delete_dp(dp_id)
        msg = DatapathDown(ct_id=self.ID, dp_id=dp_id)
        self.ipc_send(RFSERVER_RFPROXY_CHANNEL, RFSERVER_ID, msg)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def on_packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        dp_id = dp.id

        for f in msg.match.fields:
            if f.header == dp.ofproto.OXM_OF_IN_PORT:
                in_port = f.value

        # If the packet came from a switch, redirect it to the right RFVS port
        if not is_rfvs(dp_id):
            vs_port = self.table.dp_port_to_vs_port(dp_id, in_port)
            if vs_port is not None:
                vs_id, vs_port = vs_port
                switch = self.switches._get_switch(vs_id)
                if switch is not None:
                    send_pkt_out(switch.dp, vs_port, msg.data)
                    log.debug("forwarding packet to rfvs (vs_id: %s, "
                              "vs_port: %d)", dpid_to_str(vs_id), vs_port)
                else:
                    log.warn("dropped packet to rfvs (vs_id: %s, "
                             "vs_port: %d)", dpid_to_str(dp_id), in_port)
            else:
                log.info("Unmapped datapath port (dp_id=%s, dp_port=%d)",
                         dpid_to_str(dp_id), in_port)
            return

        # If we have a mapping packet, inform RFServer through a Map message
        pkt, _, _ = ethernet.parser(msg.data)
        if pkt.ethertype == RF_ETH_PROTO:
            vm_id, vm_port = struct.unpack("QB", msg.data[14:23])
            log.info("Received mapping packet (vm_id=%s, vm_port=%d, "
                     "vs_id=%s, vs_port=%d)", format_id(vm_id), vm_port,
                     dpid_to_str(dp_id), in_port)
            msg = VirtualPlaneMap(vm_id=vm_id, vm_port=vm_port, vs_id=dp_id,
                                  vs_port=in_port)
            self.ipc_send(RFSERVER_RFPROXY_CHANNEL, RFSERVER_ID, msg)
            return

        # Packet came from RFVS
        dp_port = self.table.vs_port_to_dp_port(dp_id, in_port)
        if dp_port is not None:
            dp_id, dp_port = dp_port
            switch = self.switches._get_switch(dp_id)
            if switch is not None:
                send_pkt_out(switch.dp, dp_port, msg.data)
                log.debug("forwarding packet from rfvs (dp_id: %s, "
                          "dp_port: %d)", dpid_to_str(dp_id), dp_port)
            else:
                log.warn("dropped packet from rfvs (dp_id: %s, "
                         "dp_port: %d)", dpid_to_str(dp_id), dp_port)
        else:
            log.info("Unmapped RFVS port (vs_id=%s, vs_port=%d)",
                     dpid_to_str(dp_id), in_port)
