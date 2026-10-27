import paho.mqtt.client as mqtt

BROKER_HOST = "10.20.1.57"
BROKER_PORT = 1883
USERNAME    = "admin"
PASSWORD    = "admin"

# Subscribe to everything under "muc/"
TOPIC       = "muc/energy"
CLIENT_ID   = "solvimus-subscriber"


def on_connect(client, userdata, flags, reason_code, properties=None):
    """
    Called when the client connects to the broker.
    """
    if reason_code == 0:
        print("Connected to broker, subscribing to:", TOPIC)
        client.subscribe(TOPIC)
    else:
        print("Failed to connect, reason code:", reason_code)


def on_message(client, userdata, msg):
    """
    Called when a PUBLISH message is received.
    """
    print(f"Topic: {msg.topic}")
    print(f"Payload: {msg.payload.decode('utf-8', errors='replace')}")
    print("-" * 40)


def main():
    client = mqtt.Client(client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
    client.username_pw_set(USERNAME, PASSWORD)

    client.on_connect = on_connect
    client.on_message = on_message

    print(f"Connecting to {BROKER_HOST}:{BROKER_PORT} ...")
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)

    # Loop forever, handling reconnects and incoming messages
    client.loop_forever()


if __name__ == "__main__":
    main()