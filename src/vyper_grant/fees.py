"""Quote the actual LaPoste payload; RSUP reference uses the same deployed route."""

from eth_abi import encode
from .core import call, address, ZERO


def campaign_fee(client, remote, sender, market, campaign, block="latest"):
    def read(addr, name, outputs, inputs=(), args=()):
        return call(client, addr, name, inputs, outputs, args, block)

    laposte = read(remote, "LA_POSTE", ("address",))
    factory = read(remote, "TOKEN_FACTORY", ("address",))
    adapter = read(laposte, "adapter", ("address",))
    router = read(adapter, "router", ("address",))
    gas = read(adapter, "BASE_GAS_LIMIT", ("uint256",)) + 600_000
    nonce = read(laposte, "sentNonces", ("uint256",), ("uint256",), (42161,)) + 1
    metadata = read(
        factory,
        "getTokenMetadata",
        ("string", "string", "uint8"),
        ("address",),
        (campaign[3],),
    )
    params = encode(
        [
            "(uint256,address,address,address,uint8,uint256,uint256,address[],address,bool)"
        ],
        [campaign],
    )
    payload = encode(["(uint8,address,address,bytes)"], [(0, sender, market, params)])
    message = encode(
        [
            "(uint256,address,address,(address,uint256)[],(string,string,uint8)[],bytes,uint256)"
        ],
        [
            (
                42161,
                remote,
                remote,
                [(campaign[3], campaign[6])],
                [metadata],
                payload,
                nonce,
            )
        ],
    )
    components = [
        {"name": n, "type": t}
        for n, t in [
            ("receiver", "bytes"),
            ("data", "bytes"),
            ("tokenAmounts", "tuple[]"),
            ("feeToken", "address"),
            ("extraArgs", "bytes"),
        ]
    ]
    components[2]["components"] = [
        {"name": "token", "type": "address"},
        {"name": "amount", "type": "uint256"},
    ]
    abi = [
        {
            "type": "function",
            "name": "getFee",
            "stateMutability": "view",
            "inputs": [
                {"type": "uint64", "name": "destination"},
                {"type": "tuple", "name": "message", "components": components},
            ],
            "outputs": [{"type": "uint256", "name": ""}],
        }
    ]
    cc = client.eth.contract(address=address(router), abi=abi)
    raw = cc.functions.getFee(
        4949039107694359620,
        (
            encode(["address"], [adapter]),
            message,
            [],
            ZERO,
            bytes.fromhex("97a657c9") + encode(["uint256"], [gas]),
        ),
    ).call(block_identifier=block)
    return (raw * 120 + 99) // 100
