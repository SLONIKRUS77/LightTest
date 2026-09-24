from setuptools import setup


setup(
    name="lighttest-cli",
    version="1.0.0",
    description="A lightweight command-line network speed test",
    py_modules=["main"],
    python_requires=">=3.8",
    entry_points={
        "console_scripts": [
            "lighttest=main:main",
        ],
    },
)