from ..configuration import LoadSpec
from ..exceptions import BencherException
from ._base import Bencher, register_bencher


@register_bencher
class H2loadBencher(Bencher):
    alias = "h2load"
    prc_name = "h2load"
    description = "h2load from nghttp2 (count, plain HTTP/1.1)"

    def validate_load(self, url: str, load: LoadSpec) -> None:
        """Reject unsupported modes rather than silently changing their policies."""
        super().validate_load(url, load)

        if not load.keepalive:
            raise BencherException("h2load: disabling HTTP/1.1 keepalive is unsupported")

    def build_argv(self, url: str, load: LoadSpec) -> list[str]:
        """Select HTTP/1.1 and an explicit request count and generator thread count."""
        return [
            self.binary, "--h1", "-c", f"{load.clients}", "-n", f"{load.requests}",
            "-t", f"{load.worker_threads}", url,
        ]
