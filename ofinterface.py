from rflogging import log

from rflib.defs import *
from binascii import *
from rflib.types.Match import *
from rflib.types.Action import *
from rflib.types.Option import *
from rflib.types.Band import *

OFP_BUFFER_NONE = 0xffffffff
ROUTEFLOW_COOKIE = 0x12007EF103

def actions_from_routemod(ofproto, parser, action_tlvs):
    instructions = []
    actions = []
    for a in action_tlvs:
        action = Action.from_dict(a)
        if action._type == RFAT_SET_ETH_SRC:
            srcMac = action._value
            src = parser.OFPMatchField.make(ofproto.OXM_OF_ETH_SRC, srcMac)
            actions.append(parser.OFPActionSetField(src))
        elif action._type == RFAT_SET_ETH_DST:
            dstMac = action._value
            dst = parser.OFPMatchField.make(ofproto.OXM_OF_ETH_DST, dstMac)
            actions.append(parser.OFPActionSetField(dst))
        elif action._type in (RFAT_PUSH_VLAN_ID, RFAT_SWAP_VLAN_ID):
            if action._type == RFAT_SWAP_VLAN_ID:
                actions.append(parser.OFPActionPopVlan())
            actions.append(parser.OFPActionPushVlan(0x8100));
            vlan_id = bin_to_int(action._value)
            vlan = parser.OFPMatchField.make(ofproto.OXM_OF_VLAN_VID, vlan_id|ofproto.OFPVID_PRESENT)
            actions.append(parser.OFPActionSetField(vlan))
            pcp = parser.OFPMatchField.make(ofproto.OXM_OF_VLAN_PCP, 0)
            actions.append(parser.OFPActionSetField(pcp))
        elif action._type == RFAT_OUTPUT:
            port = bin_to_int(action._value)
            actions.append(parser.OFPActionOutput(port, ofproto.OFPCML_MAX))
        elif action._type == RFAT_GROUP:
            group = bin_to_int(action._value)
            actions.append(parser.OFPActionGroup(group))
        elif action._type == RFAT_GOTO:
            table_id = bin_to_int(action._value)
            instructions.append(parser.OFPInstructionGotoTable(table_id))
        elif action._type == RFAT_STRIP_VLAN_DEFERRED:
            instructions.append(parser.OFPInstructionActions(
                ofproto.OFPIT_WRITE_ACTIONS, (parser.OFPActionPopVlan(),)))
        elif action._type == RFAT_STRIP_VLAN:
            actions.append(parser.OFPActionPopVlan())
        elif action._type == RFAT_CLEAR_DEFERRED:
            instructions.append(parser.OFPInstructionActions(
                ofproto.OFPIT_CLEAR_ACTIONS, ()))
        elif action._type == RFAT_SET_VLAN_PCP:
            pcp = bin_to_int(action._value)
            actions.append(parser.OFPActionSetField(vlan_pcp=pcp))
        elif action._type == RFAT_SET_QUEUE:
            queue = bin_to_int(action._value)
            actions.append(parser.OFPActionSetQueue(queue))
        elif action._type == RFAT_APPLY_METER:
            meter_id = bin_to_int(action._value)
            instructions.append(parser.OFPInstructionMeter(meter_id))
        elif action._type == RFAT_GROUP_DEFERRED:
            group = bin_to_int(action._value)
            instructions.append(parser.OFPInstructionActions(
                ofproto.OFPIT_WRITE_ACTIONS,
                (parser.OFPActionGroup(group),)))
        elif action._type == RFAT_SET_VLAN_ID:
            vlan_id = bin_to_int(action._value)
            vlan = parser.OFPMatchField.make(ofproto.OXM_OF_VLAN_VID, vlan_id|ofproto.OFPVID_PRESENT)
            actions.append(parser.OFPActionSetField(vlan))
        elif action.optional():
            log.info("Dropping unsupported Action (type: %s)" % action._type)
        else:
            log.warning("Failed to serialise Action (type: %s)" % action._type)
    return (actions, instructions)


def create_group_mod(dp, command, group, action_tlvs, weight=0,type_=None):
    ofproto = dp.ofproto
    parser = dp.ofproto_parser

    if command == RMT_ADD_GROUP:
        command = ofproto.OFPGC_ADD
    elif command == RMT_DELETE_GROUP:
        command = ofproto.OFPGC_DELETE

    if type_ is None:
        type_ = ofproto.OFPGT_INDIRECT

    if group == 0:
        group = ofproto.OFPG_ALL

    buckets = []
    actions, _ = actions_from_routemod(ofproto, parser, action_tlvs)
    buckets = [parser.OFPBucket(weight, actions=actions)]

    return parser.OFPGroupMod(dp, command, type_, group, buckets)


def bands_from_routemod(ofproto, parser, band_tlvs):
    bands = []
    for a in band_tlvs:
        band = Band.from_dict(a)
        if band._type == RFBT_DROP:
            bands.append(parser.OFPMeterBandDrop(
                band.get_rate(), band.get_burst_size()))
        elif band._type == RFBT_DSCP_REMARK:
            bands.append(parser.OFPMeterBandDscpRemark(
                band.get_rate(), band.get_burst_size(), band.get_prec_level()))
        elif band._type == RFBT_EXPERIMENTER:
            bands.append(parser.OFPMeterBandExperimenter(
                band.get_rate(), band.get_burst_size(), band.get_experimenter()))
        else:
            log.warning("Failed to serialise Band (type: %s)" % band._type)
    return bands


def create_meter_mod(dp, command, meter, band_tlvs, flags=None):
    ofproto = dp.ofproto
    parser = dp.ofproto_parser

    if command == RMT_ADD_METER:
        command = ofproto.OFPMC_ADD
    elif command == RMT_DELETE_METER:
        command = ofproto.OFPMC_DELETE

    if flags is None:
        flags = ofproto.OFPMF_STATS

    if meter == 0:
        meter = ofproto.OFPM_ALL

    bands = bands_from_routemod(ofproto, parser, band_tlvs)

    return parser.OFPMeterMod(dp, command, flags, meter, bands)


def create_default_flow_mod(dp, cookie=ROUTEFLOW_COOKIE, cookie_mask=0xFFFFFFFFFFFFFFFF, table_id=0,
                            command=None, idle_timeout=0, hard_timeout=0,
                            priority=None,
                            buffer_id=OFP_BUFFER_NONE, match=None, actions=None,
                            inst_type=None, out_port=None, out_group=None,
                            flags=0, inst=[]):
    ofproto = dp.ofproto
    parser = dp.ofproto_parser

    if command is None:
        command = ofproto.OFPFC_ADD

    if inst is []:
        if inst_type is None:
            inst_type = ofproto.OFPIT_APPLY_ACTIONS

        if actions is not None:
            inst = [parser.OFPInstructionActions(inst_type, actions)]

    if match is None:
        match = parser.OFPMatch()

    if out_port is None:
        out_port = ofproto.OFPP_ANY

    if out_group is None:
        out_group = ofproto.OFPG_ANY

    return parser.OFPFlowMod(dp, cookie, cookie_mask,
                             table_id, command,
                             idle_timeout, hard_timeout,
                             priority, buffer_id,
                             out_port, out_group,
                             flags, match, inst)


def create_flow_mod(dp, table_id, mod, matches, actions, options):
    ofproto = dp.ofproto
    parser = dp.ofproto_parser
    flow_mod = None
    if mod == RMT_DELETE and table_id == 0 and len(matches) == 0 and len(actions) == 0:
        return parser.OFPFlowMod(dp, ROUTEFLOW_COOKIE, 0xFFFFFFFFFFFFFFFF,
                                 ofproto.OFPTT_ALL, ofproto.OFPFC_DELETE,
                                 0, 0, 0, OFP_BUFFER_NONE,
                                 ofproto.OFPP_ANY,
                                 ofproto.OFPG_ANY,
                                 0, parser.OFPMatch(), [])
    else:
        command = add_command(ofproto, mod)
        flow_mod = create_default_flow_mod(dp, table_id=table_id, command=command)
        add_matches(flow_mod, matches)
        add_actions(flow_mod, actions)
        add_options(flow_mod, options)
        return flow_mod


def add_command(ofproto, mod):
    if mod == RMT_ADD:
        return ofproto.OFPFC_ADD
    elif mod == RMT_DELETE:
        return ofproto.OFPFC_DELETE_STRICT
    return None


def add_matches(flow_mod, matches):
    ip_proto = 0
    for m in matches:
        match = Match.from_dict(m)
        if match._type == RFMT_IPV4:
            value = bin_to_int(match._value)
            addr = value >> 32
            mask = value & ((1 << 32) - 1)
            flow_mod.match.set_dl_type(ETHERTYPE_IP)
            flow_mod.match.set_ipv4_dst_masked(addr, mask)
        elif match._type == RFMT_IPV6:
            v = match._value
            addr = tuple((ord(v[i]) << 8) | ord(v[i + 1])
                         for i in range(0, 16, 2))
            mask = tuple((ord(v[i]) << 8) | ord(v[i + 1])
                         for i in range(16, 32, 2))
            flow_mod.match.set_dl_type(ETHERTYPE_IPV6)
            flow_mod.match.set_ipv6_dst_masked(addr, mask)
        elif match._type == RFMT_ETHERNET:
            flow_mod.match.set_dl_dst(match._value)
        elif match._type == RFMT_ETHERTYPE:
            flow_mod.match.set_dl_type(bin_to_int(match._value))
        elif match._type == RFMT_NW_PROTO:
            ip_proto = bin_to_int(match._value)
            flow_mod.match.set_ip_proto(ip_proto)
        elif match._type == RFMT_TP_SRC:
            if ip_proto == IPPROTO_TCP:
                flow_mod.match.set_tcp_src(bin_to_int(match._value))
            elif ip_proto == IPPROTO_UDP:
                flow_mod.match.set_udp_src(bin_to_int(match._value))
            else:
                log.warning("Failed to match L4 proto src_port")
        elif match._type == RFMT_TP_DST:
            if ip_proto == IPPROTO_TCP:
                flow_mod.match.set_tcp_dst(bin_to_int(match._value))
            elif ip_proto == IPPROTO_UDP:
                flow_mod.match.set_udp_dst(bin_to_int(match._value))
            else:
                log.warning("Failed to match L4 proto dst_port")
        elif match._type == RFMT_IN_PORT:
            flow_mod.match.set_in_port(bin_to_int(match._value))
        elif match._type == RFMT_VLAN_ID:
            flow_mod.match.set_vlan_vid(bin_to_int(match._value))
        elif match._type == RFMT_VLAN_TAGGED:
            tagged = match.get_value()
            vid = OFPVID_PRESENT if tagged else 0
            flow_mod.match.set_vlan_vid_masked(vid, OFPVID_PRESENT)
        elif TLV.optional(match):
            log.info("Dropping unsupported Match (type: %s)" % match._type)
        else:
            log.warning("Failed to serialise Match (type: %s)" % match._type)
            return


def add_actions(flow_mod, action_tlvs):
    parser = flow_mod.datapath.ofproto_parser
    ofproto = flow_mod.datapath.ofproto
    actions, instructions = actions_from_routemod(ofproto, parser, action_tlvs)
    if len(actions) > 0:
        instructions.append(parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions))
    flow_mod.instructions = instructions


def add_options(flow_mod, options):
    for o in options:
        option = Option.from_dict(o)
        if option._type == RFOT_PRIORITY:
            flow_mod.priority = bin_to_int(option._value)
        elif option._type == RFOT_IDLE_TIMEOUT:
            flow_mod.idle_timeout = bin_to_int(option._value)
        elif option._type == RFOT_HARD_TIMEOUT:
            flow_mod.hard_timeout = bin_to_int(option._value)
        elif option._type == RFOT_CT_ID:
            pass
        elif option.optional():
            log.info("Dropping unsupported Option (type: %s)" % option._type)
        else:
            log.warning("Failed to serialise Option (type: %s)" % option._type)
            return


def send_pkt_out(dp, port, msg_data):
    ofproto = dp.ofproto
    parser = dp.ofproto_parser
    actions = [parser.OFPActionOutput(port, len(msg_data))]
    buffer_id = OFP_BUFFER_NONE
    in_port = ofproto.OFPP_CONTROLLER
    packet_out = parser.OFPPacketOut(dp,
                                     buffer_id,
                                     in_port,
                                     actions,
                                     msg_data)
    dp.send_msg(packet_out)
