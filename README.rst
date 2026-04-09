Bits - Quick Start Guide
========================

Bits is a build orchestration tool for complex software stacks. It
fetches sources, resolves dependencies, and builds packages in a
reproducible, parallel environment.

   Full documentation is available in `REFERENCE.md <REFERENCE.md>`__.
   This guide covers only the essentials.

--------------

Installation
------------

.. code:: bash

   git clone https://github.com/bitsorg/bits.git
   cd bits
   export PATH=$PWD:$PATH          # add bits to your PATH
   python -m venv .venv
   source .venv/bin/activate
   pip install -e .                # install Python dependencies

| **Requirements**: Python 3.8+, git, and `Environment
  Modules <https://modules.sourceforge.net/>`__ (``modulecmd``).
| On macOS: ``brew install modules``
| On Debian/Ubuntu: ``apt-get install environment-modules``
| On RHEL/CentOS: ``yum install environment-modules``

--------------

Quick Start (Building ROOT)
---------------------------

.. code:: bash

   # 1. Clone a recipe repository
   git clone https://github.com/bitsorg/alice.bits.git
   cd alice.bits

   # 2. Check that your system is ready
   bits doctor ROOT

   # 3. Build ROOT and all its dependencies
   bits build ROOT

   # 4. Enter the built environment
   bits enter ROOT/latest

   # 5. Run the software
   root -b

   # 6. Exit the environment
   exit

--------------

Basic Commands
--------------

+-----------------------------+-----------------------------------------+
| Command                     | Description                             |
+=============================+=========================================+
| ``bits build <pkg>``        | Build a package and its dependencies.   |
+-----------------------------+-----------------------------------------+
| ``bits enter <pkg>/latest`` | Spawn a subshell with the package       |
|                             | environment loaded.                     |
+-----------------------------+-----------------------------------------+
| ``bits load <pkg>``         | Print commands to load a module (must   |
|                             | be ``eval``\ 'd).                       |
+-----------------------------+-----------------------------------------+
| ``bits q [regex]``          | List available modules.                 |
+-----------------------------+-----------------------------------------+
| ``bits clean``              | Remove stale build artifacts.           |
+-----------------------------+-----------------------------------------+
| ``bits doctor <pkg>``       | Verify system requirements.             |
+-----------------------------+-----------------------------------------+

`Full command reference <REFERENCE.md#16-command-line-reference>`__

--------------

Configuration
-------------

Create a ``bits.rc`` file (INI format) to set defaults:

.. code:: ini

   [bits]
   organisation = ALICE

   [ALICE]
   sw_dir       = /path/to/sw          # output directory
   repo_dir     = /path/to/recipes     # recipe repository root
   search_path  = common,extra         # additional recipe dirs (appended .bits)

| Bits looks for ``bits.rc`` in: ``--config FILE`` → ``./bits.rc`` →
  ``./.bitsrc`` → ``~/.bitsrc``.
| `Configuration details <REFERENCE.md#4-configuration>`__

--------------

Writing a Recipe
----------------

`See complete recipe reference <REFERENCE.md#17-recipe-format-reference>`__

--------------

Cleaning Up
-----------

.. code:: bash

   bits clean                # remove temporary build directories
   bits clean --aggressive-cleanup   # also remove source mirrors and tarballs

`Cleaning options <REFERENCE.md#7-cleaning-up>`__

--------------

Docker & Remote Builds
----------------------

.. code:: bash

   # Build inside a Docker container for a specific Linux version
   bits build --docker --architecture ubuntu2004_x86-64 ROOT

   # Use a remote binary store (S3, HTTP, rsync) to share pre-built artifacts
   bits build --remote-store s3://mybucket/builds ROOT

`Docker support <REFERENCE.md#21-docker-support>`__ \| `Remote
stores <REFERENCE.md#20-remote-binary-store-backends>`__

--------------

Development & Testing (Contributing)
------------------------------------

.. code:: bash

   git clone https://github.com/bitsorg/bits.git
   cd bits
   python -m venv .venv
   source .venv/bin/activate
   pip install -e .[test]

   # Run tests
   tox                     # full suite on Linux
   tox -e darwin           # reduced suite on macOS
   pytest                  # fast unit tests only

`Developer guide <REFERENCE.md#part-ii--developer-guide>`__

--------------

Next Steps
----------

- `Environment management (``bits enter``, ``load``,
  ``unload``) <REFERENCE.md#6-managing-environments>`__
- `Dependency graph visualisation <REFERENCE.md#bits-deps>`__
- `Repository provider feature (dynamic recipe
  repos) <REFERENCE.md#13-repository-provider-feature>`__
- `Defaults profiles <REFERENCE.md#18-defaults-profiles>`__
- `Design principles &
  limitations <REFERENCE.md#22-design-principles--limitations>`__

--------------

**Note**: Bits is under active development. For the most up-to-date
information, see the full `REFERENCE.md <REFERENCE.md>`__.

