from uuid import UUID

import structlog
from eth_utils import to_canonical_address

from raiden.exceptions import ServiceRequestFailed
from raiden.messages.metadata import RouteMetadata
from raiden.network.pathfinding import PFSConfig, query_paths
from raiden.transfer import channel, views
from raiden.transfer.state import ChainState, ChannelState, RouteState
from raiden.utils.formatting import to_checksum_address
from raiden.utils.typing import (
    Address,
    BlockNumber,
    FeeAmount,
    InitiatorAddress,
    List,
    OneToNAddress,
    Optional,
    PaymentAmount,
    PaymentWithFeeAmount,
    PrivateKey,
    TargetAddress,
    TokenNetworkAddress,
    Tuple,
)

log = structlog.get_logger(__name__)


def get_best_routes(
    chain_state: ChainState,
    token_network_address: TokenNetworkAddress,
    one_to_n_address: Optional[OneToNAddress],
    from_address: InitiatorAddress,
    to_address: TargetAddress,
    amount: PaymentAmount,
    previous_address: Optional[Address],
    pfs_config: Optional[PFSConfig],
    privkey: PrivateKey,
) -> Tuple[Optional[str], List[RouteState], Optional[UUID]]:

    token_network = views.get_token_network_by_address(chain_state, token_network_address)
    assert token_network, "The token network must be validated and exist."

    # Always use a direct channel if available:
    # - There are no race conditions and the capacity is guaranteed to be
    #   available.
    # - There will be no mediation fees
    # - The transfer will be faster
    if Address(to_address) in token_network.partneraddresses_to_channelidentifiers.keys():
        for channel_id in token_network.partneraddresses_to_channelidentifiers[
            Address(to_address)
        ]:
            channel_state = token_network.channelidentifiers_to_channels[channel_id]

            # direct channels don't have fees
            payment_with_fee_amount = PaymentWithFeeAmount(amount)
            is_usable = channel.is_channel_usable_for_new_transfer(
                channel_state, payment_with_fee_amount, None
            )

            if is_usable is channel.ChannelUsability.USABLE:
                direct_route = RouteState(
                    route=[Address(from_address), Address(to_address)],
                    estimated_fee=FeeAmount(0),
                )
                return None, [direct_route], None

    latest_channel_opened_at = 0
    for channel_state in token_network.channelidentifiers_to_channels.values():
        latest_channel_opened_at = max(
            latest_channel_opened_at, channel_state.open_transaction.finished_block_number
        )

    if pfs_config is not None and one_to_n_address is not None:
        pfs_error_msg, pfs_routes, pfs_feedback_token = get_best_routes_pfs(
            chain_state=chain_state,
            token_network_address=token_network_address,
            one_to_n_address=one_to_n_address,
            from_address=from_address,
            to_address=to_address,
            amount=amount,
            previous_address=previous_address,
            pfs_config=pfs_config,
            privkey=privkey,
            pfs_wait_for_block=BlockNumber(latest_channel_opened_at),
        )

        if not pfs_error_msg:
            # As of version 0.5 it is possible for the PFS to return an empty
            # list of routes without an error message.
            if not pfs_routes:
                return "PFS could not find any routes", list(), None

            log.info(
                "Received route(s) from PFS", routes=pfs_routes, feedback_token=pfs_feedback_token
            )
            return pfs_error_msg, pfs_routes, pfs_feedback_token

        log.warning(
            "Request to Pathfinding Service was not successful. "
            "No routes to the target were found.",
            pfs_message=pfs_error_msg,
        )
        return pfs_error_msg, list(), None

    log.warning("Pathfinding Service could not be used.")
    return "Pathfinding Service could not be used.", list(), None


def get_best_routes_pfs(
    chain_state: ChainState,
    token_network_address: TokenNetworkAddress,
    one_to_n_address: OneToNAddress,
    from_address: InitiatorAddress,
    to_address: TargetAddress,
    amount: PaymentAmount,
    previous_address: Optional[Address],
    pfs_config: PFSConfig,
    privkey: PrivateKey,
    pfs_wait_for_block: BlockNumber,
) -> Tuple[Optional[str], List[RouteState], Optional[UUID]]:
    try:
        pfs_routes, feedback_token = query_paths(
            pfs_config=pfs_config,
            our_address=chain_state.our_address,
            privkey=privkey,
            current_block_number=chain_state.block_number,
            token_network_address=token_network_address,
            one_to_n_address=one_to_n_address,
            chain_id=chain_state.chain_id,
            route_from=from_address,
            route_to=to_address,
            value=amount,
            pfs_wait_for_block=pfs_wait_for_block,
        )
    except ServiceRequestFailed as e:
        log_message = ("PFS: " + e.args[0]) if e.args[0] else None
        log_info = e.args[1] if len(e.args) > 1 else {}
        log.warning("An error with the path request occurred", log_message=log_message, **log_info)
        return log_message, [], None

    paths = []
    for path_object in pfs_routes:
        path = path_object["path"]
        estimated_fee = path_object["estimated_fee"]
        canonical_path = [to_canonical_address(node) for node in path]

        # get the second entry, as the first one is the node itself
        # also needs to be converted to canonical representation
        partner_address = canonical_path[1]

        # don't route back
        if partner_address == previous_address:
            continue

        channel_state = views.get_channelstate_by_token_network_and_partner(
            chain_state=chain_state,
            token_network_address=token_network_address,
            partner_address=partner_address,
        )

        if not channel_state:
            continue

        # check channel state
        if channel.get_status(channel_state) != ChannelState.STATE_OPENED:
            log.info(
                "Channel is not opened, ignoring",
                from_address=to_checksum_address(from_address),
                partner_address=to_checksum_address(partner_address),
                routing_source="Pathfinding Service",
            )
            continue

        paths.append(
            RouteState(
                route=canonical_path,
                estimated_fee=estimated_fee,
            )
        )

    return None, paths, feedback_token


def resolve_routes(
    routes: List[RouteMetadata],
    token_network_address: TokenNetworkAddress,
    chain_state: ChainState,
) -> List[RouteState]:
    """resolve the forward_channel_id for a given route

    TODO: We don't have ``forward_channel_id``, anymore. Does this function still make sense?
    """

    resolvable = []
    for route_metadata in routes:
        if len(route_metadata.route) < 2:
            continue

        channel_state = views.get_channelstate_by_token_network_and_partner(
            chain_state=chain_state,
            token_network_address=token_network_address,
            partner_address=route_metadata.route[1],
        )

        if channel_state is not None:
            resolvable.append(
                RouteState(
                    route=route_metadata.route,
                    # This is only used in the mediator, so fees are set to 0
                    estimated_fee=FeeAmount(0),
                )
            )
    return resolvable
