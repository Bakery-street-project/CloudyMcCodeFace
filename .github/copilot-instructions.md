# CloudyMcCodeFace Development Instructions

Always reference these instructions first and fallback to search or bash commands only when you encounter unexpected information that does not match the info here.

## Current Repository State

This repository is currently in its initial setup phase with only GitHub configuration files. It is configured to support multi-language development including JavaScript/Node.js, Go, Python, and GitHub Actions.

## Working Effectively

### Initial Repository Setup
- Clone repository: `git clone https://github.com/Bakery-street-projct/CloudyMcCodeFace.git`
- Navigate to repository: `cd CloudyMcCodeFace`
- Check current state: `git --no-pager status`
- View repository structure: `ls -la`

### Available Development Tools
The following tools are pre-installed and validated:
- Node.js v20.19.5 with npm v10.8.2
- Go v1.24.7
- Python 3.12.3 with pip
- Git version control
- Make build system

### Multi-Language Project Setup

#### JavaScript/Node.js Projects
When JavaScript code is added to this repository:
- Initialize Node.js project: `npm init -y`
- Install dependencies: `npm install` -- typically takes 1-3 minutes. NEVER CANCEL. Set timeout to 10+ minutes.
- Build project: `npm run build` -- build time varies by project size. NEVER CANCEL. Set timeout to 30+ minutes for large projects.
- Run tests: `npm test` -- test time varies by test suite size. NEVER CANCEL. Set timeout to 20+ minutes.
- Start development server: `npm run dev` or `npm start`
- Lint code: `npm run lint` (if configured)
- Format code: `npm run format` (if configured)

#### Go Projects
When Go code is added to this repository:
- Initialize Go module: `go mod init github.com/Bakery-street-projct/CloudyMcCodeFace`
- Download dependencies: `go mod download` -- typically takes 2-5 minutes. NEVER CANCEL. Set timeout to 15+ minutes.
- Build project: `go build` -- build time varies by project size. NEVER CANCEL. Set timeout to 30+ minutes for large projects.
- Run tests: `go test ./...` -- test time varies by test suite size. NEVER CANCEL. Set timeout to 20+ minutes.
- Format code: `go fmt ./...`
- Vet code: `go vet ./...`
- Run application: `go run main.go` (if main.go exists)

#### Python Projects
When Python code is added to this repository:
- Create virtual environment: `python3 -m venv venv`
- Activate virtual environment: `source venv/bin/activate`
- Install dependencies: `pip install -r requirements.txt` -- typically takes 2-5 minutes. NEVER CANCEL. Set timeout to 15+ minutes.
- Run tests: `python -m pytest` or `python -m unittest` -- test time varies by test suite size. NEVER CANCEL. Set timeout to 20+ minutes.
- Run application: `python main.py` (if main.py exists)
- Lint code: `flake8` or `pylint` (if configured)
- Format code: `black` (if configured)

### Git Operations
- Check repository status: `git --no-pager status`
- View commit history: `git --no-pager log --oneline`
- View changes: `git --no-pager diff`
- Create new branch: `git checkout -b feature/branch-name`
- Add changes: `git add .`
- Commit changes: `git commit -m "descriptive commit message"`
- Push changes: `git push origin branch-name`

## Validation Requirements

### Before Making Changes
- ALWAYS run `git --no-pager status` to understand current repository state
- ALWAYS check for existing configuration files (package.json, go.mod, requirements.txt, etc.)
- ALWAYS validate that any build or test commands work before committing changes

### After Making Changes
- ALWAYS run project-specific linting and formatting tools
- ALWAYS run existing test suites to ensure no regressions
- ALWAYS validate that build processes still work
- ALWAYS commit changes with descriptive commit messages

### Manual Validation Scenarios
Since this repository supports multiple languages, when code is added:

**JavaScript Projects:**
- Test the application startup process
- Verify all API endpoints work (if it's a web service)
- Test the build output in a production-like environment

**Go Projects:**
- Test the CLI application with `./app --help` and sample commands
- Verify the application handles expected input/output correctly
- Test concurrent operations if applicable

**Python Projects:**
- Test the application with sample input data
- Verify the application produces expected output formats
- Test error handling with invalid inputs

## Repository Structure

### Current Files
```
.github/
├── CODEOWNERS          # Code ownership definitions
├── FUNDING.yml         # Funding configuration
├── SECURITY.md         # Security policy
└── dependabot.yml      # Dependency update configuration
```

### Expected Structure (when code is added)
```
.github/
├── workflows/          # GitHub Actions workflows
├── CODEOWNERS
├── FUNDING.yml
├── SECURITY.md
└── dependabot.yml

src/                    # Source code directory
├── javascript/         # Node.js/JavaScript projects
├── go/                 # Go projects
└── python/             # Python projects

tests/                  # Test suites
docs/                   # Documentation
README.md               # Project documentation
```

## Important Configuration Files

### Dependabot Configuration
The repository is configured for automatic dependency updates for:
- JavaScript (weekly updates)
- Go (weekly updates) 
- Python (weekly updates)
- GitHub Actions (weekly updates)

### Code Ownership
- Primary maintainer: BoozeLee
- All changes require code owner review

## Common Development Workflows

### Adding a New Feature
1. Create feature branch: `git checkout -b feature/feature-name`
2. Implement changes following language-specific best practices
3. Add or update tests for new functionality
4. Run full test suite: `[language-specific test command]` -- NEVER CANCEL. Set timeout to 30+ minutes.
5. Run linting and formatting tools
6. Commit changes: `git commit -m "Add feature: descriptive message"`
7. Push branch: `git push origin feature/feature-name`
8. Create pull request for code review

### Bug Fixes
1. Create bugfix branch: `git checkout -b bugfix/issue-description`
2. Write test that reproduces the bug
3. Implement fix
4. Verify test now passes
5. Run full test suite to ensure no regressions
6. Commit and push changes
7. Create pull request

### Release Process
When the repository grows to include release workflows:
1. Update version numbers in appropriate files
2. Run full test suite: `[language-specific test command]` -- NEVER CANCEL. Set timeout to 30+ minutes.
3. Build project: `[language-specific build command]` -- NEVER CANCEL. Set timeout to 45+ minutes.
4. Create release tag: `git tag -a v1.0.0 -m "Release version 1.0.0"`
5. Push tag: `git push origin v1.0.0`

## Critical Timing Expectations

- **Repository clone**: 30 seconds - 2 minutes
- **Dependency installation**: 1-15 minutes depending on project size
- **Build processes**: 1-45 minutes depending on project complexity
- **Test suites**: 1-30 minutes depending on test coverage
- **NEVER CANCEL** long-running operations without waiting at least 60 minutes
- **ALWAYS** set timeouts to at least double the expected time

## Security Considerations

- Follow security policy outlined in `.github/SECURITY.md`
- Never commit secrets or sensitive information
- Use environment variables for configuration
- Regularly update dependencies through dependabot
- Report security vulnerabilities to security@example.com

## Troubleshooting

### Common Issues
- **Build failures**: Check for missing dependencies or outdated tool versions
- **Test failures**: Ensure test environment matches expected configuration
- **Git issues**: Use `git --no-pager status` and `git --no-pager log --oneline` to diagnose

### Getting Help
- Check existing issues and pull requests
- Reference language-specific documentation
- Contact code owners for repository-specific questions