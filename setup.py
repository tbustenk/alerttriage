"""Package setup for AlertTriage v2."""

from setuptools import setup, find_packages

with open("requirements.txt") as fh:
    install_requires = [
        line.strip()
        for line in fh
        if line.strip() and not line.startswith("#")
    ]

setup(
    name="alerttriage",
    version="2.0.0",
    description="Production-grade AI alert analysis platform",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/tbustenk/alerttriage",
    packages=find_packages(exclude=["tests*", "scripts*"]),
    python_requires=">=3.11",
    install_requires=install_requires,
    extras_require={
        "dev": [
            "pytest>=8.0",
            "pytest-asyncio>=0.23",
            "ruff>=0.4",
            "mypy>=1.10",
            "types-PyYAML",
            "pip-audit>=2.7",
        ]
    },
    entry_points={
        "console_scripts": [
            "alerttriage-init=alerttriage.scripts.init_client:main",
            "alerttriage-run=alerttriage.scripts.run_client:main",
        ]
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "License :: OSI Approved :: MIT License",
        "Intended Audience :: Information Technology",
        "Topic :: Security",
        "Typing :: Typed",
    ],
)
