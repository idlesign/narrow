import re

from ._base import Bencher, register_bencher
from ..exceptions import BencherException
from ..utils import run_command, get_logger

RE_RPS = re.compile('finished in [^,]+, [^,]+, (\d+) req/s,')
RE_STATUS = re.compile('status codes: (\d+) 2xx, (\d+) 3xx, (\d+) 4xx, (\d+) 5xx')

LOG = get_logger(__name__)


@register_bencher
class WeighttpBencher(Bencher):
    """
    https://github.com/lighttpd/weighttp

    """
    alias = 'weighttp'
    prc_name = 'weighttp'
    description = 'weighttp (no SSL support)'

    def get_version(self):
        versions = '%s' % run_command(
            '%s -v' % self.prc_name
        ).replace(' - a lightweight and simple webserver benchmarking tool', '')

        return versions

    @classmethod
    def fire(cls, url, *, num_clients, num_requests):

        out = run_command(
            '%s -c %s -n %s %s' % (cls.prc_name, num_clients, num_requests, url))

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
