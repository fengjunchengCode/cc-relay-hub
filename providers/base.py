from abc import ABC, abstractmethod


class MessageProvider(ABC):
    @abstractmethod
    def deliver(self, envelope):
        raise NotImplementedError

    @abstractmethod
    def poll_events(self, cursor=None):
        raise NotImplementedError

    @abstractmethod
    def get_health(self):
        raise NotImplementedError


class ControlProvider(ABC):
    def supports_control(self):
        return False

    def execute_command(self, command):
        raise NotImplementedError
