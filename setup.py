from setuptools import setup
import os

VERSION = "0.1"


def get_long_description():
    with open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md"),
        encoding="utf8",
    ) as fp:
        return fp.read()


setup(
    name="datasette-litestream",
    description="",
    long_description=get_long_description(),
    long_description_content_type="text/markdown",
    author="Alex Garcia",
    url="https://github.com/asg017/datasette-litestream",
    project_urls={
        "Issues": "https://github.com/asg017/datasette-litestream/issues",
        "CI": "https://github.com/asg017/datasette-litestream/actions",
        "Changelog": "https://github.com/asg017/datasette-litestream/releases",
    },
    license="Apache License, Version 2.0",
    classifiers=[
        "Framework :: Datasette",
        "License :: OSI Approved :: Apache Software License",
    ],
    version=VERSION,
    packages=["datasette_litestream"],
    entry_points={
        "datasette": ["litestream = datasette_litestream"]
    },
    package_data={"datasette_litestream": ["templates/*.html"]},
    install_requires=["datasette"],
    extras_require={"test": ["pytest", "pytest-asyncio"]},
    python_requires=">=3.7",
)
