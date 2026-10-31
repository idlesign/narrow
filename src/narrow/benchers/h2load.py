import re

from ..exceptions import BencherException
from ..utils import get_logger, run_command
from ._base import Bencher, register_bencher

RE_RPS = re.compile(r"finished in [^,]+, (\d+.\d+) req/s,")
RE_STATUS = re.compile(r"status codes: (\d+) 2xx, (\d+) 3xx, (\d+) 4xx, (\d+) 5xx")

LOG = get_logger(__name__)


@register_bencher
class H2loadBencher(Bencher):
    """
    https://github.com/nghttp2/nghttp2/

    """
    alias = 'h2load'
    prc_name = 'h2load'
    description = 'h2load from nghttp2'

    def get_version(self) -> str:
        return run_command(f"{self.prc_name} --version")

    @classmethod
    def fire(cls, url: str, *, num_clients: int, num_requests: int) -> str:

        out = run_command(
            f"{cls.prc_name} --h1 -c {num_clients} -n {num_requests} {url}")

        match_rps = RE_RPS.search(out)
        match_status = RE_STATUS.search(out)

        if not match_rps or not match_status:
            LOG.debug(out)
            raise BencherException(f"Failed to parse results data:\n{out}")

        success_count = int(match_status.group(1))

        if num_requests != success_count:
            LOG.debug(out)

            raise BencherException(
                'No successful responses.\n'
                'Check server is running, use --log command to dump logs and investigate.')

        rps = match_rps.group(1)

        return rps
