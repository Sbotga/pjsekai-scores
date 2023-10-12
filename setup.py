import os
import setuptools

version = os.environ.get("CI_COMMIT_TAG", "v0")

setuptools.setup(version=version)
