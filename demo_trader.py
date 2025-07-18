from pocketoptionapi_async import AsyncPocketOptionClient
import asyncio

async def main():
    SSID = '42["auth",{"session":"j8okmjo61k8icgdhcmvd9ffm6g","isDemo":1,"uid":92118257,"platform":2,"isFastHistory":true,"isOptimized":true}]'
    client = AsyncPocketOptionClient(SSID, is_demo=True)
    await client.connect()
    balance = await client.get_balance()
    print(f'this is your balance: {balance}')
    from pocketoptionapi_async import OrderDirection
    order = await client.place_order(
        asset="EURUSD_otc",
        amount=1.0,
        direction=OrderDirection.CALL,
        duration=60
    )
    print(order.order_id, order.status)

    await client.disconnect()

asyncio.run(main())
