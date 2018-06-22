import io
import os
import re

from setuptools import setup, find_packages


PATH_BASE = os.path.dirname(__file__)


def read_file(fpath):
    """Reads a file within package directories."""
    with io.open(os.path.join(PATH_BASE, fpath)) as f:
        return f.read()


def get_version():
    """Returns version number, without module import (which can lead to ImportError
    if some dependencies are unavailable before install."""
    contents = read_file(os.path.join('narrow', '__init__.py'))
    version = re.search('VERSION = \(([^)]+)\)', contents)
    version = version.group(1).replace(', ', '.').strip()
    return version


setup(
    name='narrow',
    version=get_version(),
    url='https://github.com/idlesign/narrow',

    description='Naive throughput measurements for Python web apps and servers',
    long_description=read_file('README.rst'),
    license='BSD 3-Clause License',

    author='Igor Starikov',
    author_email='idlesign@yandex.ru',

    packages=find_packages(),
    include_package_data=True,
    zip_safe=False,

    install_requires=[
        'uwsgi',
        'uwsgiconf>=0.14.2',
        'click',
        'plotly',
        'envbox',
        'colorhash',
    ],
    setup_requires=[],

    entry_points={
        'console_scripts': ['narrow = narrow.cli:main'],
    },

    test_suite='tests',

    classifiers=[
        # As in https://pypi.python.org/pypi?:action=list_classifiers
        'Development Status :: 4 - Beta',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'License :: OSI Approved :: BSD License'
    ],
)


