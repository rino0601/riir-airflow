from nicegui import ui

from ..atoms.message import message
from ..templates.theme import frame


class ClassExample:
    def __init__(self) -> None:
        """The page is created as soon as the class is instantiated.

        This can obviously also be done in a method, if you want to decouple the instantiation of the object from the page creation.
        """

        @ui.page("/b")
        def page_b():
            with frame("- Page B -"):
                message("Page B")
                ui.label("This page is defined in a class.")
