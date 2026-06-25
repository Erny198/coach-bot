class Application:
    @staticmethod
    def builder():
        raise NotImplementedError

class CallbackQueryHandler: ...
class CommandHandler:
    def __init__(self, *a, **k): ...
class ContextTypes:
    DEFAULT_TYPE = object
class MessageHandler:
    def __init__(self, *a, **k): ...

class _Filter:
    def __and__(self, other): return self
    def __invert__(self): return self

class filters:
    TEXT = _Filter()
    COMMAND = _Filter()
    VOICE = _Filter()
