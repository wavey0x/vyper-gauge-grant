# pragma version ==0.4.3
# The system mock holds gauge and controller state together.
gauge: immutable(address)

@deploy
def __init__(system: address):
    gauge = system

@external
@raw_return
def __default__() -> Bytes[96]:
    return raw_call(gauge, msg.data, max_outsize=96, is_static_call=True)
