from nicegui import app, ui

from ..atoms.message import message
from ..templates.theme import frame


class HomePage:
    def __init__(self) -> None:
        """The page is created as soon as the class is instantiated.

        This can obviously also be done in a method, if you want to decouple the instantiation of the object from the page creation.
        """

        @ui.page("/")
        def index_page() -> None:
            with frame("Homepage"):
                ui.label("Hello, FastAPI!")
                message("This is the home page.").classes("font-bold")
                ui.label("Use the menu on the top right to navigate.")
                # NOTE dark mode will be persistent for each user across tabs and server restarts
                ui.dark_mode().bind_value(app.storage.user, "dark_mode")
                ui.checkbox("dark mode").bind_value(app.storage.user, "dark_mode")
