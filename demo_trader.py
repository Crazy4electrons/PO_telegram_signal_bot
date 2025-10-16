from http import client
from pocketoptionapi_async import AsyncPocketOptionClient
import asyncio
from pocketoptionapi_async import AsyncPocketOptionClient, OrderDirection
client=None
order = None
async def main():
    global client
    global order
    SSID = '42["auth",{"session":"j8okmjo61k8icgdhcmvd9ffm6g","isDemo":1,"uid":92118257,"platform":2,"isFastHistory":true,"isOptimized":true}]'
    client = AsyncPocketOptionClient(SSID, is_demo=True, enable_logging=False)
    await client.connect()
    
    while True:
        command = input("next command:check_win,get_active_orders,check_order_results,disconnect: ").strip().lower()
        if command == "check_win":
            await check_win(order.order_id)
        elif command == "get_active_orders":
            await get_active_orders()
        elif command == "check_order_results":
            await check_order_results()
        elif command == "place_order":
            await place_order()
        elif command == "disconnect":
            await disconnect_client()
            break
        else:
            print("Unknown command")

async def place_order():
    global client
    global order
    amount = 1
    symbol = "USD/CNH"
    direction = OrderDirection.PUT
    order = await client.place_order(asset=symbol, amount=amount, direction=direction, duration=60)
    print(f"Order placed successfully: {order}")


async def check_win(order_id):
    global client
    global order
    check_win = await client.check_win(order.order_id)
    
    if check_win:
        print(f"Order {order_id} was successful!")
    else:
        print(f"Order {order_id} did not win.")
    print(f"Order details: {order}")
    
async def get_active_orders():
    global client
    active_orders = await client.get_active_orders()
    
    if active_orders:
        print("Active Orders:")
        for order in active_orders:
            print(order)
    else:
        print("No active orders found.")    
        
    
async def check_order_results():
    global client
    global order
    if order:
        result = await client.check_order_result(order.order_id)
        if result:
            print(f"Order Result: {result}")
        else:
            print("No result found for the order.")
    else:
        print("No order to check results for.")

async def disconnect_client():
    global client
    if client:
        await client.disconnect()
        print("Disconnected from Pocket Option client.")
    else:
        print("Client is not connected.")

asyncio.run(main())