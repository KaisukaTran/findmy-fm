from fastapi import FastAPI
from findmy.api.common.handlers import value_error_handler
from findmy.api.common.middleware import trace_id_middleware


app = FastAPI()


app.add_exception_handler(ValueError, value_error_handler)


app.middleware("http")(trace_id_middleware)