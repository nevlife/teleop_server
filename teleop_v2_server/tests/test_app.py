import asyncio

from aiohttp.test_utils import TestClient, TestServer

from teleop_v2_server.app import create_app
from teleop_v2_server.config import ServerConfig


def test_pair_relay_and_exclusive_client():
    async def scenario():
        app = create_app(ServerConfig())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            rover = await client.ws_connect("/ws")
            await rover.send_json(
                {"type": "hello", "protocol_version": 2, "role": "rover", "robot_id": "r1"}
            )
            assert (await rover.receive_json())["type"] == "hello_ack"

            operator = await client.ws_connect("/ws")
            await operator.send_json(
                {"type": "hello", "protocol_version": 2, "role": "client", "robot_id": "r1"}
            )
            assert (await operator.receive_json())["type"] == "hello_ack"
            rover_ready = await rover.receive_json()
            operator_ready = await operator.receive_json()
            assert rover_ready["create_offer"] is True
            assert operator_ready["create_offer"] is False
            assert rover_ready["session_id"] == operator_ready["session_id"]
            assert isinstance(rover_ready["session_epoch"], str)

            await rover.send_json({"type": "offer", "sdp": "v=0"})
            forwarded = await operator.receive_json()
            assert forwarded == {"type": "offer", "sdp": "v=0", "from_role": "rover"}

            duplicate = await client.ws_connect("/ws")
            await duplicate.send_json(
                {"type": "hello", "protocol_version": 2, "role": "client", "robot_id": "r1"}
            )
            error = await duplicate.receive_json()
            assert error["code"] == "role_in_use"
            await duplicate.close()
            await operator.close()
            assert (await rover.receive_json())["type"] == "peer_left"
            await rover.close()
        finally:
            await client.close()

    asyncio.run(scenario())
