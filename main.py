import asyncio
from datetime import datetime

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from pony import orm


async def homepage(request):
    return Response(
        "Hello world! And welcome to Bast's Pixel Write Exchange!\n"
        "All requests should have the 'Authorization' header set to a unique identifiable token of up to 30 characters that will be used for your balance. Surrounding spaces will be stripped.\n"
        "GET /tasks to get the top ten highest paying tasks. You may provide ?minimum_pay=<float> to filter.\n"
        '\tFormat: {"id": task_id, "pay": task_pay, "x": x_coord, "y": y_coord, "color": hex_color}\n'
        "GET /tasks/<taskid> to claim a task. This claim will last 30 seconds.\n"
        "POST /tasks/<taskid> to submit a task. We will verify whether the pixel has changed, and reward you with your payment.\n"
        "\tWe check every 10 seconds (or roughly the maximum view ratelimit) for new pixels globally, and faster with /get_pixel on individual submissions if available. It may take up to that long for your submission to return, so plan accordingly.\n"
        "POST /tasks to create a task. This endpoint accepts a JSON request in the same format as is returned from a GET from /tasks.\n"
    )


db = orm.Database()


class User(db.Entity):
    id = orm.PrimaryKey(int, auto=True)
    identifier = orm.Required(str, index=True)
    money = orm.Required(float, sql_default=0)
    total_tasks = orm.Required(int, sql_default=0)
    requested_tasks = orm.Set('Task', reverse='reservation')
    created_tasks = orm.Set('Task', reverse='creator')


class Task(db.Entity):
    id = orm.PrimaryKey(int, auto=True)
    creator = orm.Required(User)
    completed = orm.Required(bool, sql_default=False)
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
                asyncio.create_task(expire_task(task.id, task.reservation_expires))


async def expire_task(task_id: int, time: datetime):
    time_to_sleep = (datetime.now() - datetime).total_seconds()
    await asyncio.sleep(time_to_sleep)
    with orm.db_session():
        task = Task[task_id]
        if not task.completed:
            task.reservation = None
            task.reservation_task_id = None
            task.reservation_expires = None
        else:
            return  # Successfully completed while we waited

app = Starlette(
    debug=True,
    routes=[
        Route('/', homepage),
    ],
    on_startup=[start_database],
)
orm.set_sql_debug(True)
