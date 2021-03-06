import asyncio
import random
from datetime import datetime, timedelta
from typing import Optional


import aiohttp
import numpy as np
from dotenv import dotenv_values
from PIL import Image
from pony import orm
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


# NOTES ON WORKING WITH PONY AND ASYNCIO:
#  DO NOT AWAIT WITHIN orm.db_session() or YOU WILL CAUSE DEADLOCK OR CORRUPTION
#  Keep transaction windows short and sweet, like normal except more so.

RETURNED_TASK_COUNT = 48  # Number of tasks to return on GET /tasks
EXPIRATION_OFFSET = timedelta(minutes=1)
MINIMUM_WAGE = 0.1
MAX_PASS_LENGTH = 128
# these are dynamically updated on a timer
CANVAS_WIDTH = 242
CANVAS_HEIGHT = 153
CANVAS_REFRESH_RATE = 10  # seconds
API_BASE = "https://pixels.pythondiscord.com"
CONFIG = dotenv_values(".env")

API_KEY = CONFIG["API_KEY"]
INFO_WEBHOOK = CONFIG["INFO_WEBHOOK"]
MAGIC_AUTHORIZATION = CONFIG["MAGIC_AUTHORIZATION"]
HEADERS = {
    "Authorization": "Bearer " + API_KEY,
    "User-Agent": "bast-write-market/0.1",
}


async def homepage(request):
    return Response(
        "Hello world! And welcome to Bast's Pixel Write Exchange!\n"
        "\nThis service is currently DISABLED because the pixels event has ended!\n"
        "We will eventually be moving this endpoint to pixels.subsystems.bast-development.com\n"
        f"\nAll requests to the pixex api should have the 'Authorization' header set to a unique identifiable token of up to {MAX_PASS_LENGTH} characters that will be used to keep track of your accounting!"
        " Surrounding spaces will be stripped.\n"
        f"\nGET /tasks to get the top {RETURNED_TASK_COUNT} highest paying tasks. You may provide ?minimum_pay=<float> to filter.\n"
        '\tReturns: [{"id": task_id, "pay": task_pay},]\n'
        "\nGET /tasks/<taskid> to claim a task. This claim will last 1 minute.\n"
        '\tReturns: {"id": task_id, "pay": task_pay, "x": x_coord, "y": y_coord, "color": hex_color, "expires": expiration_time}\n'
        "\nPOST /tasks/<taskid> to submit a task. We will verify whether the pixel has changed, and reward you with your payment.\n"
        "\tWe check every 10 seconds (or roughly the maximum view ratelimit) for new pixels globally, and faster with /get_pixel on individual submissions if available. "
        "It may take up to that long for your submission to return, so plan accordingly.\n"
        "\nPOST /tasks to create a task.\n"
        '\tFormat: {"pay": task_pay, "x": x_coord, "y": y_coord, "color": hex_color}\n'
        '\tReturns: {"id": new_task_id}\n'
        "\nGET /balance to view your balance\n"
        '\tReturns: {"id": your_id, "balance": your_balance}\n'
        "\nDELETE /tasks/<task_id> to delete a task you've submitted. This will return an error if it's already been reserved.\n"
        f"\n\nGetting started:\nAdd an 'Authorization: your-secret-code-here (make it yourself! treat it like a password. Longest it can be is {MAX_PASS_LENGTH} characters)' header to a requests.get() and hit /balance and /tasks."
        "\nThen GET /tasks/<the-task-id-you-want> to reserve it.\n"
        "Set the pixel, then POST /tasks/<the-task-id-you-want>. We will check it and award points! There's no json content neccessary or anything!\n"
        "\n\nGET /tasks/stats to view global statistics, and provide your Authoriation header to view your specific statistics too!\n"
        '\tReturns: {'
        '\n\t\t"available": available_tasks, '
        '\n\t\t"all_completed": total_completed_tasks,'
        '\n\t\t"all_reserved": total_reserved_tasks,'
        '\n\t}\n'
        '\tWith token: {'
        '\n\t\t"reserved": my_currently_reserved_tasks,'
        '\n\t\t"completed": my_completed_tasks,'
        '\n\t\t"average_pay": my_average_pay,'
        '\n\t\t"total_earnings": my_total_earnings,'
        '\n\t\t"submitted": my_submissions,'
        '\n\t\t"services_provided": my_completed_submissions,'
        '\n\t\t"paid": my_completed_payments,'
        '\n\t\t"waiting_payments": my_pending_payments,'
        '\n\t\t"waiting": my_pending_tasks,'
        '\n\t\t"deleted": my_deleted_tasks,'
        '\n\t}'
        '\n\naverage_pay may be `null` if you have not completed any tasks so far.\n'
    )
    # Delete endpoint works for magic auth freely, and returns the money to the magic account
    # "POST /balance/<user_id>" to add money to a user with the magic api token, useful for fixing the economy. Requires the integer amount in the request body


async def fetch_tasks(request):
    minimum_pay = request.query_params.get('minimum_pay')
    if minimum_pay:
        minimum_pay = float(minimum_pay)
    else:
        minimum_pay = MINIMUM_WAGE

    with orm.db_session():
        top_ten_payers = orm.select(task for task in Task if not task.completed and not task.deleted and not task.reservation and task.pay >= minimum_pay).order_by(orm.desc(Task.pay))[:RETURNED_TASK_COUNT]
        top_ten_payers = [{"id": task.id, "pay": task.pay} for task in top_ten_payers]
    return JSONResponse(top_ten_payers)


async def task_stats(request):
    authorization = request.headers.get('Authorization', None)
    if authorization and len(authorization.strip()) > MAX_PASS_LENGTH:
        return Response(f"Auth tokens must be {MAX_PASS_LENGTH} characters or less in size", status_code=401)

    with orm.db_session():
        response = {
            "available": orm.count(task for task in Task if not task.completed and not task.deleted and not task.reservation),
            "all_completed": orm.count(task for task in Task if task.completed and not task.deleted),
            "all_reserved": orm.count(task for task in Task if task.reservation and not task.completed and not task.deleted),
        }

        if authorization:
            user = User.get(identifier=authorization.strip())
            response.update({
                "average_pay": orm.avg(task.pay for task in Task if task.completed and task.reservation == user),
                "total_earnings": orm.sum(task.pay for task in Task if task.completed and task.reservation == user),
                "completed": orm.count(task for task in Task if task.completed == user and not task.deleted),
                "reserved": orm.count(task for task in Task if task.reservation == user and not task.completed and not task.deleted),
                "submitted": orm.count(task for task in Task if task.creator == user),
                "services_provided": orm.count(task for task in Task if task.creator == user and task.completed and not task.deleted),
                "paid": orm.sum(task.pay for task in Task if task.creator == user and not task.completed),
                "waiting_payments": orm.sum(task.pay for task in Task if task.creator == user and not task.reservation and not task.deleted and not task.completed),
                "waiting": orm.count(task for task in Task if task.creator == user and not task.reservation and not task.deleted and not task.completed),
                "deleted": orm.count(task for task in Task if task.creator == user and task.deleted),
            })
            if response["completed"] >= 1000:
                response["message"] = "Glad to have you here! The 1k club is proud of it's members!"
            if response["submitted"] > 10:
                response["submitters_club"] = "Welcome! Enjoy your stay!"

    return JSONResponse(response)


def enforce_auth(function):
    async def wrapped(request):
        authorization = request.headers.get('Authorization', None)
        if not authorization or not authorization.strip():
            return Response("Authorization is required for this endpoint", status_code=401)
        elif len(authorization.strip()) > MAX_PASS_LENGTH:
            return Response(f"Auth tokens must be {MAX_PASS_LENGTH} characters or less in size", status_code=401)

        return await function(request)
    return wrapped


@enforce_auth
async def create_task(request):
    authorization = request.headers.get('Authorization', None)

    try:
        data = await request.json()
    except json.decoder.JSONDecodeError:
        return Response("Invalid content: your json could not be decoded", status_code=400)

    invalid_keys = set(data) - {'id', 'pay', 'x', 'y', 'color'}
    if invalid_keys:
        return Response("Invalid keys in data: " + " ,".join(invalid_keys), status_code=400)

    if 'x' not in data:
        return Response("Invalid offer: You need to tell me the x coordinate for the pixel", status_code=400)

    try:
        x = int(data['x'])
        if x < 0:
            return Response(f"Invalid x value '{x}': must be greater than or equal to zero", status_code=400)
        if x >= CANVAS_WIDTH:
            return Response(f"Invalid x value '{x}': must be less than {CANVAS_WIDTH}", status_code=400)
    except ValueError:
        return Response(f"Invalid x value '{data['x']}': must be convertible to an integer", status_code=400)

    if 'y' not in data:
        return Response("Invalid offer: You need to tell me the y coordinate for the pixel", status_code=400)

    try:
        y = int(data['y'])
        if y < 0:
            return Response(f"Invalid y value '{y}': must be greater than or equal to zero", status_code=400)
        if y >= CANVAS_HEIGHT:
            return Response(f"Invalid y value '{y}': must be less than {CANVAS_HEIGHT}", status_code=400)
    except ValueError:
        return Response(f"Invalid y value '{data['y']}': must be convertible to an integer", status_code=400)

    if 'color' not in data:
        return Response("Invalid offer: You need to tell me what color the pixel should be set to in 6-char hex format", status_code=400)


    color = data['color'].strip().lower()
    if len(color) != 6:
        return Response(f"Invalid color '{color}': colors must be 6 characters long", status_code=400)

    bad_chars = set(color) - set("0123456789abcdef")
    if bad_chars:
        return Response(f"Invalid color: '{color}' must not have the characters '{repr(''.join(bad_chars))}")

    if 'pay' not in data:
        return Response("Invalid offer: You need to tell me how much they should be paid", status_code=400)

    try:
        pay = float(data['pay'])
    except ValueError:
        return Response("Invalid payment offer: must be convertible to a number", status_code=400)

    if pay < MINIMUM_WAGE:
        return Response(f"Invalid pay '{pay}': you can't offer pay below the minimum wage of {MINIMUM_WAGE} cats per pixel.", status_code=400)

    with orm.db_session():
        user = User.get_from_authorization(authorization)
        if user.money < pay:
            return Response("Invalid payment offer: pay must be less than what you current have banked.", status_code=400)

        new_task = Task(
            creator=user,
            x=x,
            y=y,
            color=color,
            pay=pay,
        )

        user.money -= pay

    response_json = {"id": new_task.id}
    if random.random() < 0.5:
        response_json["message"] = "Thanks for making the world a better place!"

    await log("New task created!", id=new_task.id, x=x, y=y, pay=pay, color=color, user=user.id)

    return JSONResponse(response_json)


@enforce_auth
async def reserve_task(request):
    authorization = request.headers.get('Authorization', None)

    task_id = request.path_params['task_id']

    return JSONResponse("Reservations can no longer be made as pixels has temporarily concluded! Check back later for our reopening.", status_code=404)

    with orm.db_session():
        user = User.get_from_authorization(authorization)
        task = Task.get(id=task_id)
        if not task:
            return Response(f"Invalid reserve request: task id '{task_id}' does not exist.", status_code=400)

        if task.completed:
            return Response(f"That task (id '{task.id}') has already been completed.", status_code=400)

        if task.reservation:
            return Response(f"That task (id '{task.id}') has already been reserved.", status_code=410)

        task.reservation = user
        task.reservation_expires = datetime.utcnow() + EXPIRATION_OFFSET
        expiration_task = asyncio.create_task(expire_task(task.id, task.reservation_expires))
        task.reservation_task_id = reserve_task.NEXT_TASK_ID
        reserve_task.EXPIRATION_TASKS[task.reservation_task_id] = expiration_task
        reserve_task.NEXT_TASK_ID += 1

    await log("Task reserved!", id=task.id, x=task.x, y=task.y, pay=task.pay, color=task.color, by=user.id)

    return JSONResponse({"id": task.id, "x": task.x, "y": task.y, "color": task.color, "pay": task.pay, "expires": task.reservation_expires.isoformat()+"Z"})

reserve_task.NEXT_TASK_ID = 1
reserve_task.EXPIRATION_TASKS = {}


@enforce_auth
async def balance(request):
    authorization = request.headers.get('Authorization', None)

    with orm.db_session():
        user = User.get_from_authorization(authorization)

    return JSONResponse({"id": user.id, "balance": user.money})


@enforce_auth
async def fix_economy(request):
    authorization = request.headers.get('Authorization', None)

    if not authorization.strip() == MAGIC_AUTHORIZATION:
        return Response("As if it were so easy. Go fulfill some requests, or if there are none, yell @bast.", status_code=403)

    user_id = request.path_params['user_id']

    amount = float(await request.body())

    with orm.db_session():
        was = User[user_id].money
        new = was + amount
        User[user_id].money = new

    await log("User balance updated:", id=user_id, was=was, now=new, added=amount)

    return JSONResponse({"id": user_id, "now": new, "was": was, "added": amount})


@enforce_auth
async def delete_task(request):
    authorization = request.headers.get('Authorization', None)

    task_id = request.path_params['task_id']

    with orm.db_session():
        user = User.get_from_authorization(authorization)
        task = Task.get(id=task_id)
        magic = authorization.strip() == MAGIC_AUTHORIZATION

        if not task:
            return Response(f"Task id '{task_id}' does not exist", status_code=400)

        if task.completed:
            return Response(f"Task id '{task_id}' has already been completed", status_code=410)

        if task.reservation and not magic:
            return Response(f"Task id '{task_id}' is reserved, so you cannot delete it", status_code=403)

        if task.creator != user:
            return Response(f"Task id '{task_id}' was not created by you", status_code=403)

        task.deleted = True
        task.completed = user
        user.money += task.pay

    await log("Task deleted:", id=user.id, task=task.id, created_by=task.creator.id)

    return Response(f"Task id '{task_id}' successfully deleted. You have been refunded the '{task.pay}' cats you paid for your placement")



async def update_canvas():
    global CURRENT_CANVAS, CANVAS_UPDATED_AT
    if (CANVAS_UPDATED_AT - datetime.now()).total_seconds() < CANVAS_REFRESH_RATE:
        CANVAS_UPDATED_AT = datetime.now()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_BASE + "/get_pixels", headers=HEADERS) as response:
                response.raise_for_status()
                current_pixels_raw = await response.read()
        print("Got %s pixel fragments", len(current_pixels_raw))
    except Exception:
        print(response)
        await log("Failed to update canvas view:", response=str(response), code=response.status)
        return None
    current_pixels = np.frombuffer(current_pixels_raw, dtype=np.uint8)
    pixels = np.reshape(current_pixels, (CANVAS_HEIGHT, CANVAS_WIDTH, 3))
    CURRENT_CANVAS = pixels


CURRENT_CANVAS = None
CANVAS_UPDATED_AT = datetime.now()
pixel_resets_by = datetime.now()


@enforce_auth
async def submit_task(request):
    global pixel_resets_by
    authorization = request.headers.get('Authorization', None)

    task_id = request.path_params['task_id']

    with orm.db_session():
        user = User.get_from_authorization(authorization)
        task = Task.get(id=task_id)

        if not task:
            return Response(f"There is no task with id '{task_id}'", status_code=400)

        if task.reservation and task.reservation != user:
            return Response("You are not the user who reserved this task", status_code=403)

    if pixel_resets_by - datetime.now() < timedelta(seconds=10):
        print("Waiting until get_pixel cooldown resets")
        await asyncio.sleep((pixel_resets_by - datetime.now()).total_seconds())
    async with aiohttp.ClientSession() as session:
        async with session.get(API_BASE + "/get_pixel", params={"x": task.x, "y": task.y}, headers=HEADERS) as response:
            data = await response.json()
            requests_left = response.headers.get("requests-remaining")
            requests_reset = response.headers.get("requests-reset")
            cooldown_reset = response.headers.get("cooldown-reset")

    if cooldown_reset:
        # fuck
        pixel_resets_by = datetime.now() + timedelta(seconds=int(cooldown_reset))
        await log("Hit the fucking /get_pixel ratelimit", remaining=cooldown_reset, requests_left=requests_left, reset=requests_reset)
        await asyncio.sleep(float(cooldown_reset))
        # Wait and try again for our user
        return await submit_task(request)

    if requests_left == "0":
        pixel_resets_by = datetime.now() + timedelta(seconds=int(requests_reset))

    color = data["rgb"]
    if color == task.color:
        # Success!
        with orm.db_session():
            task = Task.get(id=task_id)
            user = User[user.id]
            task.completed = user
            user.money += task.pay

        await log("Pixel completed!", x=task.x, y=task.y, color=task.color, user=user.id)
        return Response(f"Congratulations and thank you for your efforts! You have been paid {task.pay} cats for this pixel!")

    return Response(f"The pixel at {task.x}, {task.y} appears to currently be {color}, not {task.color}. /get_pixel may take up to a second to update, so feel free to try again. Otherwise someone may have sniped your pixel ;-;. Sorry. Feel free to try again later.", status_code=404)


db = orm.Database()


class User(db.Entity):
    id = orm.PrimaryKey(int, auto=True)
    identifier = orm.Required(str, index=True, unique=True)
    money = orm.Required(float, default=0)
    total_tasks = orm.Required(int, default=0)
    requested_tasks = orm.Set('Task', reverse='reservation')
    created_tasks = orm.Set('Task', reverse='creator')
    completed_tasks = orm.Set('Task', reverse='completed')

    @classmethod
    def get_from_authorization(cls, authorization: str) -> 'User':
        authorization = authorization.strip()
        user = cls.get(identifier=authorization)
        if not user and authorization == MAGIC_AUTHORIZATION:
            # initial user seed
            return cls(identifier=authorization, money=30)
        return user or cls(identifier=authorization)


class Task(db.Entity):
    id = orm.PrimaryKey(int, auto=True)
    creator = orm.Required(User)
    completed = orm.Optional(User)
    deleted = orm.Required(bool, default=False)
    x = orm.Required(int)
    y = orm.Required(int)
    color = orm.Required(str)
    pay = orm.Required(float)
    reservation = orm.Optional(User)
    reservation_expires = orm.Optional(datetime)
    reservation_task_id = orm.Optional(int)  # name of the asyncio task we use to cancel auto-expire


async def start_database():
    db.bind(provider='sqlite', filename='data.db', create_db=True)
    db.generate_mapping(create_tables=True)

    with orm.db_session():
        task_expiration_checks = orm.select(task for task in Task if task.reservation)
        for task in task_expiration_checks:
            assert task.reservation_expires is not None
            assert task.reservation_task_id is not None
            if task.reservation_expires < datetime.now():
                task.reservation = None
                task.reservation_expires = None
                task.reservation_task_id = None
            else:
                create_erroring_task(expire_task(task.id, task.reservation_expires))


async def expire_task(task_id: int, when: datetime):
    time_to_sleep = (when - datetime.utcnow()).total_seconds()
    await asyncio.sleep(max(time_to_sleep, 0))
    with orm.db_session():
        task = Task[task_id]
        if not task.completed:
            reserver = task.reservation
            task.reservation = None
            task.reservation_expires = None
            if task.reservation_task_id in reserve_task.EXPIRATION_TASKS:
                task_task = reserve_task.EXPIRATION_TASKS[task.reservation_task_id]
                del reserve_task.EXPIRATION_TASKS[task.reservation_task_id]
                task_task.cancel()
            task.reservation_task_id = None
        else:
            print("Successfully completed while we waited")
            return  # Successfully completed while we waited

    await log("Task reservation expired", id=task.id, x=task.x, y=task.y, pay=task.pay, color=task.color, by=reserver)



async def canvas_size_loop():
    global CANVAS_WIDTH, CANVAS_HEIGHT
    TICK_RATE = 10  # every 10 seconds
    first = True
    while True:
        if first:
            first = False
        else:
            await asyncio.sleep(TICK_RATE)
        await asyncio.sleep(1)
        async with aiohttp.ClientSession() as session:
            async with session.get(API_BASE + "/get_size", headers=HEADERS) as response:
                if response.status != 200:
                    await session.post(INFO_WEBHOOK, json=make_embed("Error hit while getting canvas size:", status_code=response.status, error=await result.read()))
                    continue
                try:
                    result = await response.json()
                except Exception as e:
                    await session.post(INFO_WEBHOOK, json=make_embed("Error while parsing /get_size json", error=str(e)))
                    continue

                if (CANVAS_WIDTH, CANVAS_HEIGHT) != (result["width"], result["height"]):
                    CANVAS_WIDTH = result["width"]
                    CANVAS_HEIGHT = result["height"]

                    await log("Setting canvas size:", width=CANVAS_WIDTH, height=CANVAS_HEIGHT)


async def start_size_loop():
    return
    create_erroring_task(canvas_size_loop())


def create_erroring_task(coroutine):
    task = asyncio.create_task(coroutine)

    def ensure_exception(fut: asyncio.Future) -> None:
        """Ensure an exception in a task is raised without hard awaiting."""
        if fut.done() and not fut.cancelled():
            return
        fut.result()
    task.add_done_callback(ensure_exception)


def make_embed(content: str, **kwargs):
    """Quick and dirty make a discord embed dictionary"""
    embed = {}
    if content:
        embed["description"] = content
    if kwargs:
        embed["fields"] = []
    for key, value in kwargs.items():
        embed['fields'].append({"name": key, "value": str(value), "inline": True})
    embed['timestamp'] = datetime.utcnow().isoformat()+"Z"
    return {"embeds": [embed]}


async def log(content: str, **kwargs):
    """Logging convenience method"""
    print("Logging:", content, kwargs)
    #async with aiohttp.ClientSession() as session:
    #    await session.post(INFO_WEBHOOK, json=make_embed(content=content, **kwargs))

async def log_startup():
    await log("Server is coming up!")

app = Starlette(
    debug=True,
    routes=[
        Route('/', homepage),
        Route('/tasks', fetch_tasks, methods=['GET']),
        Route('/tasks', create_task, methods=['POST']),
        Route('/tasks/{task_id:int}', reserve_task, methods=['GET']),
        Route('/tasks/{task_id:int}', submit_task, methods=['POST']),
        Route('/tasks/stats', task_stats, methods=['GET']),
        Route('/balance', balance, methods=['GET']),
        Route('/balance/{user_id:int}', fix_economy, methods=['POST']),
        Route('/tasks/{task_id:int}', delete_task, methods=['DELETE']),
    ],
    on_startup=[start_database, start_size_loop, log_startup],
)
#orm.set_sql_debug(True)
