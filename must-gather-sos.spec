Name:           must-gather-sos
Version:        0.1.0
Release:        4%{?dist}
Summary:        An sos-style front end for 'oc adm must-gather'

License:        Apache-2.0
URL:            https://github.com/dsedgmen/must-gather-sos
# Source tarball is produced from a git checkout by .copr/Makefile
# (git archive --prefix=<name>-<version>/ HEAD); there is no upstream
# release URL yet.
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  python3-pip
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  pyproject-rpm-macros

# %%check exercises the in-pod progress library (data/task_common.sh) by
# running it, so the shell tools it uses have to be in the buildroot.
BuildRequires:  bash
BuildRequires:  /usr/bin/awk
BuildRequires:  /usr/bin/flock

%description
must-gather-sos is an independent, community side-project. It is not
provided, maintained, or supported by Red Hat, and comes with no warranty
or support of any kind.

It brings sos report-style plugin selection to 'oc adm must-gather': skip
or select individual gather plugins instead of running all of them, and
narrow collection with --since.

%prep
%autosetup -n %{name}-%{version}

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files must_gather_sos

install -Dm644 share/man/must-gather-sos.1 \
    %{buildroot}%{_mandir}/man1/must-gather-sos.1
install -Dm644 share/completion/must-gather-sos.bash \
    %{buildroot}%{_datadir}/bash-completion/completions/must-gather-sos
install -Dm644 share/completion/_must-gather-sos \
    %{buildroot}%{_datadir}/zsh/site-functions/_must-gather-sos

%check
# Package unit tests only. tests.test_catalog_sync exercises tools/
# (PyYAML, git clones) and belongs in .github/workflows/catalog-compat.yml.
%{python3} - <<'PY'
import pathlib, unittest
loader = unittest.TestLoader()
suite = unittest.TestSuite()
for path in sorted(pathlib.Path("tests").glob("test_*.py")):
    if path.name == "test_catalog_sync.py":
        continue
    suite.addTests(loader.loadTestsFromName(f"tests.{path.stem}"))
result = unittest.TextTestRunner(verbosity=1).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
PY

%files -f %{pyproject_files}
%{_bindir}/must-gather-sos
%{_mandir}/man1/must-gather-sos.1*
%{_datadir}/bash-completion/completions/must-gather-sos
%{_datadir}/zsh/site-functions/_must-gather-sos

%changelog
* Mon Nov 9 2026 David Sedgmen <dsedgmen@redhat.com> - 0.1.0-4
- Keep tools/catalog tests out of %%check; they run in catalog-compat CI.

* Mon Nov 9 2026 David Sedgmen <dsedgmen@redhat.com> - 0.1.0-3
- BuildRequires python3-pyyaml so %%check can import tools/catalog_registry.

* Mon Nov 9 2026 David Sedgmen <dsedgmen@redhat.com> - 0.1.0-2
- Ship setup.cfg so EL9 setuptools can build the wheel (was UNKNOWN-0.0.0).

* Wed Oct 22 2026 Robin Cernin <rcernin@redhat.com> - 0.1.0-1
- Initial packaging.
