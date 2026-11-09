# sailbox-service Specification

## Purpose

Define boot-time supervision and retained exit evidence for the existing Sailbox application launcher.

## Requirements

### Requirement: Supervised Sailbox application
The Sailbox deployment SHALL start the existing protected launcher as a foreground systemd service in a dedicated non-management slice. It SHALL retain Sailbox resource enforcement, restart an exited application with a bounded restart rate, and allow an explicit service stop to remain stopped.

#### Scenario: Application exits unexpectedly
- **WHEN** the Codex LB application exits and the service has not exceeded its restart limit
- **THEN** systemd starts the launcher again after the configured restart delay
- **AND** the journal retains the application's exit result

#### Scenario: Operator stops the application
- **WHEN** the operator stops the service
- **THEN** systemd sends SIGINT to the application process group and allows a bounded graceful shutdown
- **AND** the service remains stopped

### Requirement: Explicit boot conversion
Staging the service SHALL validate its units and enable startup without starting the application or rebooting the Sailbox. The installer SHALL refuse to run while the existing application is listening. A non-systemd guest SHALL receive Sail's canonical systemd setup before staging, and SHALL require an explicit Sailbox reboot to activate systemd.

#### Scenario: Existing listener is still active
- **WHEN** port 2455 is listening during service staging
- **THEN** staging fails before changing service files

### Requirement: Bounded persistent exit evidence
The service SHALL send stdout and stderr to the system journal. Persistent and runtime journal storage SHALL have explicit size limits, and systemd SHALL survive termination of the application's service cgroup.

#### Scenario: Cold reboot after an application failure
- **WHEN** the Sailbox cold-boots from its existing disk
- **THEN** retained journal files remain available within the configured storage budget
