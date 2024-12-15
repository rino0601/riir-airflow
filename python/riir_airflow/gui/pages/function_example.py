from nicegui import ui

from ..atoms.message import message
from ..templates.theme import frame


class FunctionExample:
    def __init__(self) -> None:
        """The page is created as soon as the class is instantiated.

        This can obviously also be done in a method, if you want to decouple the instantiation of the object from the page creation.
        """

        @ui.page("/a")
        def page_a():
            with frame("- Page A -"):
                message("Page A")
                ui.label("This page is defined in a function.")
