from functools import partial

import plotly
import plotly.graph_objs as go
from colorhash import ColorHash

from .configuration import Settings

ipython = False

if ipython:

    plotly.offline.init_notebook_mode(connected=True)
    plot_method = partial(plotly.offline.iplot, filename=Settings.FILENAME_STATS_PLOT)

else:
    plot_method = partial(plotly.offline.plot, filename=Settings.FILENAME_STATS_PLOT)


def plot(stats):
    scatters = []

    def new_line(title, results):

        data_rps = []
        data_clients = []

        for result_item in results:
            rps = result_item.rps
            if rps is None:  # no data gathered
                continue
            data_rps.append(rps)
            data_clients.append(result_item.clients)

        scatter = go.Scatter(
            x=data_clients,
            y=data_rps,
            mode='lines',
            name=title,
            hoverinfo='x+y+name',
            line={
                'shape': 'spline',

                # for stable coloring across runs
                'color': ColorHash(title).hex,
            },
        )

        return scatter

    stats_meta = stats['meta']
    stats_items = stats['items']

    for realm_title, realm_results in stats_items.items():
        line = new_line(realm_title, realm_results)
        scatters.append(line)

    date = stats_meta['date']
    versions = stats_meta['versions']

    result = plot_method({
        'data': scatters,
        'layout': go.Layout(

            title='Throughput.<br>Gathered on %s UTC.<br>%s.' % (date, ' | '.join(versions)),
            xaxis={
                'title': 'Clients count',
            },
            yaxis={
                'title': 'RPS',
            },
            legend={
                'orientation': 'h',
            },
            hoverlabel={
                'namelength': -1,
            },
        )
    })

    return result
