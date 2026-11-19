from setuptools import setup
from setuptools.dist import Distribution
from wheel.bdist_wheel import bdist_wheel as _bdist_wheel


class BinaryDistribution(Distribution):
    """Distribution that is always treated as having binary extensions."""

    def has_ext_modules(self):
        # Tell setuptools/wheel: this distribution has native code.
        return True

    def is_pure(self):
        # Explicitly say it's not a pure-Python package.
        return False


class bdist_wheel(_bdist_wheel):
    def finalize_options(self):
        super().finalize_options()
        # Make sure wheel metadata says "Root-Is-Purelib: false"
        self.root_is_pure = False


setup(
    distclass=BinaryDistribution,
    cmdclass={"bdist_wheel": bdist_wheel},
)
