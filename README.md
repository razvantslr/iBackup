iBackup – Functional Requirements (v0.1)
1. Purpose
The application manages, organizes, and backs up iPhone photo/video files from multiple sources (iPhone, existing backups) into a structured, deduplicated, and incremental archive on Windows.

2. Scope (Initial Version)
The initial version focuses on safe analysis and indexing of media files without modifying originals.

3. Supported Media Types
Images: JPG, JPEG, PNG, HEIC
Videos: MP4, MOV

4. Core Concepts
Source: Any folder containing media files (iPhone import, old backups).
Destination: One or more backup folders.
Index: A persistent record of known media files.
Dry-run: No file system modifications.

5. Functional Requirements
FR-01: Folder Scanning
The system shall recursively scan a given folder.
Only supported media file types shall be processed.
FR-02: Metadata Extraction
For each media file, the system shall extract:
Absolute file path
File size (bytes)
File hash (content-based)
Date taken (from EXIF DateTimeOriginal if available)
File extension / media type
GPS coordinates if available (future use)
FR-03: Hashing
Each file shall be identified by a cryptographic hash.
Hashing shall be chunk-based to support large files.
FR-04: Observation-Only Mode (Initial)
The system shall not modify, move, copy, or delete files.
All operations shall be read-only.
FR-05: Error Handling
Corrupt or unreadable files shall be skipped.
Errors shall be logged without stopping the scan.

6. Non-Functional Requirements
NFR-01: Safety
No destructive operation by default.
NFR-02: Performance
Must handle large folders (10k+ files).
NFR-03: Extensibility
Design must allow future features:
Incremental backup
Deduplication
Conversion (HEIC → JPG)
UI

7. Out of Scope (v0.1)
Copying or deleting files
UI
Cloud sync
Reverse geocoding (GPS → city)