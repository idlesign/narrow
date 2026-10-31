from ..configuration import LoadSpec
from ._base import Bencher, register_bencher


@register_bencher
class WeighttpBencher(Bencher):
    alias = "weighttp"
    prc_name = "weighttp"
    version_flag = "-v"
    description = "weighttp (count, plain HTTP/1.1)"

    def build_argv(self, url: str, load: LoadSpec) -> list[str]:
        """Request persistent connections only when selected by the workload."""
        argv = [
            self.binary, "-c", f"{load.clients}", "-n", f"{load.requests}",
            "-t", f"{load.worker_threads}",
        ]

        if load.keepalive:
            argv.append("-k")

        return [*argv, url]
