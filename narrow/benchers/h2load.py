import re

from ._base import Bencher, register_bencher
from ..exceptions import BencherException
from ..utils import run_command, get_logger

RE_RPS = re.compile('finished in [^,]+, (\d+.\d+) req/s,')
RE_STATUS = re.compile('status codes: (\d+) 2xx, (\d+) 3xx, (\d+) 4xx, (\d+) 5xx')

LOG = get_logger(__name__)


@register_bencher
class H2loadBencher(Bencher):
    """
    https://github.com/nghttp2/nghttp2/

    """
    alias = 'h2load'
    prc_name = 'h2load'
    description = 'h2load from nghttp2'

    def get_version(self):
        return run_command('%s --version' % self.prc_name)

    @classmethod
    def fire(cls, url, *, num_clients, num_requests):

        out = run_command(
            '%s --h1 -c %s -n %s %s' % (cls.prc_name, num_clients, num_requests, url))

        match_rps = RE_RPS.search(out)
        match_status = RE_STATUS.search(out)

        if not match_rps or not match_status:
            LOG.debug(out)
            raise BencherException('Failed to parse results data:\n%s' % out)

        success_count = int(match_status.group(1))

        if num_requests != success_count:
            LOG.debug(out)

            raise BencherException(
                'No successful responses.\n'
                'Check server is running, use --log command to dump logs and investigate.')

        rps = match_rps.group(1)

        return rps
