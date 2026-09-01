#!/usr/bin/env python3

import argparse
import struct
import sys
from pathlib import Path


FILE_HEADER_SIZE = 34

EXPECTED_FILE_TAG = "GESTUS DATA FILE"
SUPPORTED_VERSIONS = {"V1.00"}

PREAMBLE = b"\x5A\xA5"

TYPE_GNSS = 0x0
TYPE_IMU = 0x1
TYPE_STATS = 0xF


class GestusFormatError(Exception):
    """Raised when the input file does not match the expected GESTUS format."""
    pass


def crc16_ccitt(data: bytes) -> int:
    """
    CRC16-CCITT parameters used by GESTUS:

        Polynomial : 0x1021
        Initial    : 0xFFFF
        RefIn      : False
        RefOut     : False
        XorOut     : 0x0000
    """

    crc = 0xFFFF

    for byte in data:
        crc ^= byte << 8

        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF

    return crc


def split_gestus_file(input_file: Path) -> None:

    file_size = input_file.stat().st_size

    if file_size < FILE_HEADER_SIZE:
        raise GestusFormatError(
            f"File is too short: {file_size} bytes. "
            f"A valid GESTUS file requires at least {FILE_HEADER_SIZE} bytes."
        )

    with input_file.open("rb") as f:

        # ============================================================
        # 1. FIXED FILE HEADER
        # ============================================================

        fixed_header = f.read(FILE_HEADER_SIZE)

        if len(fixed_header) != FILE_HEADER_SIZE:
            raise GestusFormatError(
                "Unable to read the complete GESTUS file header."
            )

        file_tag_raw = fixed_header[0:24].rstrip(b"\x00")
        version_raw = fixed_header[24:32].rstrip(b"\x00")

        try:
            file_tag_text = file_tag_raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise GestusFormatError(
                "Invalid File Tag: field is not valid ASCII."
            ) from exc

        try:
            version_text = version_raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise GestusFormatError(
                "Invalid format version: field is not valid ASCII."
            ) from exc

        # INFO_LEN is Big-Endian
        info_len = struct.unpack(">H", fixed_header[32:34])[0]

        # ============================================================
        # 2. VALIDATE FILE HEADER
        # ============================================================

        if file_tag_text != EXPECTED_FILE_TAG:
            raise GestusFormatError(
                f"Invalid GESTUS File Tag.\n"
                f"Expected : '{EXPECTED_FILE_TAG}'\n"
                f"Found    : '{file_tag_text}'"
            )

        if version_text not in SUPPORTED_VERSIONS:
            raise GestusFormatError(
                f"Unsupported GESTUS file format version: '{version_text}'. "
                f"Supported version(s): {', '.join(sorted(SUPPORTED_VERSIONS))}"
            )

        print(f"File tag     : {file_tag_text}")
        print(f"Version      : {version_text}")
        print(f"INFO_LEN     : {info_len} bytes")

        # ============================================================
        # 3. RECORDING INFORMATION
        # ============================================================

        recording_info = b""
        info_crc_status = "Not present"

        if info_len > 0:

            recording_info = f.read(info_len)

            if len(recording_info) != info_len:
                raise GestusFormatError(
                    "Incomplete Recording Information block. "
                    f"Expected {info_len} bytes, "
                    f"received {len(recording_info)}."
                )

            info_crc_raw = f.read(2)

            if len(info_crc_raw) != 2:
                raise GestusFormatError(
                    "Recording Information CRC is missing or truncated."
                )

            stored_info_crc = struct.unpack(">H", info_crc_raw)[0]
            calculated_info_crc = crc16_ccitt(recording_info)

            if stored_info_crc == calculated_info_crc:
                info_crc_status = f"OK (0x{stored_info_crc:04X})"
            else:
                info_crc_status = (
                    f"ERROR "
                    f"(stored=0x{stored_info_crc:04X}, "
                    f"calculated=0x{calculated_info_crc:04X})"
                )

        data_start_offset = f.tell()

        if data_start_offset >= file_size:
            raise GestusFormatError(
                "The file contains a valid header but no data records."
            )

        print(f"Data offset  : 0x{data_start_offset:X}")
        print(f"Header CRC   : {info_crc_status}")

        # ============================================================
        # 4. OUTPUT FILES
        # ============================================================

        base = input_file.with_suffix("")

        header_file = Path(str(base) + "_header.txt")
        gnss_file = Path(str(base) + "_gnss.bin")
        imu_file = Path(str(base) + "_imu.bin")

        gnss_records = 0
        imu_records = 0

        gnss_bytes = 0
        imu_bytes = 0

        crc_errors = 0
        resync_events = 0

        stats_offset = None

        # ============================================================
        # 5. PARSE SENSOR RECORDS
        # ============================================================

        with gnss_file.open("wb") as gnss_out, \
             imu_file.open("wb") as imu_out:

            while True:

                record_offset = f.tell()

                preamble = f.read(2)

                if not preamble:
                    break

                if len(preamble) < 2:
                    raise GestusFormatError(
                        f"Unexpected end of file at offset "
                        f"0x{record_offset:X}: incomplete record preamble."
                    )

                # ----------------------------------------------------
                # Preamble validation / resynchronisation
                # ----------------------------------------------------

                if preamble != PREAMBLE:

                    resync_events += 1

                    print(
                        f"Warning: invalid preamble at "
                        f"offset 0x{record_offset:X}. "
                        f"Searching for next record...",
                        file=sys.stderr
                    )

                    f.seek(record_offset + 1)
                    continue

                # ----------------------------------------------------
                # DATA INFO
                # ----------------------------------------------------

                data_info_raw = f.read(2)

                if len(data_info_raw) != 2:
                    raise GestusFormatError(
                        f"Incomplete Data Info field at "
                        f"offset 0x{record_offset:X}."
                    )

                data_info = struct.unpack(">H", data_info_raw)[0]

                data_type = (data_info >> 12) & 0x0F
                payload_length = data_info & 0x0FFF

                # ----------------------------------------------------
                # RECORDING STATISTICS
                # ----------------------------------------------------

                if data_type == TYPE_STATS:

                    stats_offset = record_offset

                    print(
                        f"Recording statistics found at "
                        f"offset 0x{record_offset:X}"
                    )

                    break

                # ----------------------------------------------------
                # Validate record type
                # ----------------------------------------------------

                if data_type not in (TYPE_GNSS, TYPE_IMU):
                    raise GestusFormatError(
                        f"Unsupported record type 0x{data_type:X} "
                        f"at offset 0x{record_offset:X}."
                    )

                # ----------------------------------------------------
                # PAYLOAD
                # ----------------------------------------------------

                payload = f.read(payload_length)

                if len(payload) != payload_length:
                    raise GestusFormatError(
                        f"Incomplete payload at offset "
                        f"0x{record_offset:X}. "
                        f"Expected {payload_length} bytes, "
                        f"received {len(payload)}."
                    )

                # ----------------------------------------------------
                # PAYLOAD CRC
                # ----------------------------------------------------

                crc_raw = f.read(2)

                if len(crc_raw) != 2:
                    raise GestusFormatError(
                        f"Missing payload CRC at offset "
                        f"0x{record_offset:X}."
                    )

                stored_crc = struct.unpack(">H", crc_raw)[0]
                calculated_crc = crc16_ccitt(payload)

                if stored_crc != calculated_crc:

                    crc_errors += 1

                    print(
                        f"Warning: CRC error at offset "
                        f"0x{record_offset:X}: "
                        f"type=0x{data_type:X}, "
                        f"length={payload_length}, "
                        f"stored=0x{stored_crc:04X}, "
                        f"calculated=0x{calculated_crc:04X}",
                        file=sys.stderr
                    )

                # ----------------------------------------------------
                # GNSS
                # ----------------------------------------------------

                if data_type == TYPE_GNSS:

                    gnss_out.write(payload)

                    gnss_records += 1
                    gnss_bytes += payload_length

                # ----------------------------------------------------
                # IMU
                # ----------------------------------------------------

                elif data_type == TYPE_IMU:

                    imu_out.write(payload)

                    imu_records += 1
                    imu_bytes += payload_length

        # ============================================================
        # 6. BASIC RESULT VALIDATION
        # ============================================================

        if gnss_records == 0 and imu_records == 0:
            raise GestusFormatError(
                "No GNSS or IMU records were found in the file."
            )

        # ============================================================
        # 7. TEXT HEADER / PROCESSING REPORT
        # ============================================================

        with header_file.open("w", encoding="utf-8") as header_out:

            header_out.write("GESTUS DATA FILE HEADER\n")
            header_out.write("=======================\n\n")

            header_out.write(
                f"Source file       : {input_file.name}\n"
            )

            header_out.write(
                f"Source file size  : {file_size} bytes\n\n"
            )

            header_out.write(
                f"File Tag          : {file_tag_text}\n"
            )

            header_out.write(
                f"Format Version    : {version_text}\n"
            )

            header_out.write(
                f"INFO_LEN          : {info_len} bytes\n"
            )

            header_out.write(
                f"Data start offset : "
                f"0x{data_start_offset:X} "
                f"({data_start_offset})\n\n"
            )

            header_out.write("Recording Information\n")
            header_out.write("---------------------\n")

            if info_len == 0:

                header_out.write("Not present\n")

            else:

                try:
                    info_text = recording_info.decode("utf-8")
                    header_out.write(info_text + "\n")

                except UnicodeDecodeError:
                    header_out.write(
                        recording_info.hex(" ") + "\n"
                    )

                header_out.write(
                    f"CRC: {info_crc_status}\n"
                )

            header_out.write("\nExtracted sensor data\n")
            header_out.write("---------------------\n")

            header_out.write(
                f"GNSS records      : {gnss_records}\n"
            )

            header_out.write(
                f"GNSS payload size : {gnss_bytes} bytes\n"
            )

            header_out.write(
                f"IMU records       : {imu_records}\n"
            )

            header_out.write(
                f"IMU payload size  : {imu_bytes} bytes\n"
            )

            header_out.write(
                f"CRC errors        : {crc_errors}\n"
            )

            header_out.write(
                f"Resync events     : {resync_events}\n"
            )

            if stats_offset is not None:

                header_out.write(
                    f"Statistics offset : "
                    f"0x{stats_offset:X} "
                    f"({stats_offset})\n"
                )

        # ============================================================
        # 8. SUMMARY
        # ============================================================

        print()
        print("GESTUS file successfully processed")
        print("----------------------------------")
        print(f"Input file      : {file_size} bytes")
        print(f"GNSS records    : {gnss_records}")
        print(f"GNSS payload    : {gnss_bytes} bytes")
        print(f"IMU records     : {imu_records}")
        print(f"IMU payload     : {imu_bytes} bytes")
        print(f"CRC errors      : {crc_errors}")
        print(f"Resync events   : {resync_events}")

        if stats_offset is not None:
            print(
                f"Statistics      : found at "
                f"0x{stats_offset:X}"
            )

        print()
        print("Generated files:")
        print(f"  {header_file}")
        print(f"  {gnss_file}")
        print(f"  {imu_file}")


def build_argument_parser() -> argparse.ArgumentParser:

    parser = argparse.ArgumentParser(
        prog="gestus_split.py",
        description=(
            "Extract GNSS and IMU binary streams from a "
            "GESTUS .dat recording file."
        ),
        epilog=(
            "Example:\n"
            "  python gestus_split.py "
            "B24354_rec_20260813105336397.dat\n\n"
            "Generated files:\n"
            "  <input>_header.txt   Human-readable file information\n"
            "  <input>_gnss.bin     Raw GNSS payload stream\n"
            "  <input>_imu.bin      Raw IMU payload stream"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "input_file",
        type=Path,
        help="Path to the GESTUS .dat recording file"
    )

    return parser


def main() -> int:

    parser = build_argument_parser()

    args = parser.parse_args()

    input_file = args.input_file

    # ================================================================
    # INPUT VALIDATION
    # ================================================================

    if not input_file.exists():

        print(
            f"ERROR: input file does not exist:\n"
            f"  {input_file}",
            file=sys.stderr
        )

        return 2

    if not input_file.is_file():

        print(
            f"ERROR: input path is not a file:\n"
            f"  {input_file}",
            file=sys.stderr
        )

        return 2

    if input_file.suffix.lower() != ".dat":

        print(
            f"ERROR: expected a .dat file, received:\n"
            f"  {input_file.name}",
            file=sys.stderr
        )

        return 2

    # ================================================================
    # PROCESS FILE
    # ================================================================

    try:

        split_gestus_file(input_file)

    except PermissionError as exc:

        print(
            f"ERROR: permission denied:\n"
            f"  {exc}",
            file=sys.stderr
        )

        return 3

    except GestusFormatError as exc:

        print(
            f"ERROR: invalid or corrupted GESTUS file:\n"
            f"  {exc}",
            file=sys.stderr
        )

        return 4

    except OSError as exc:

        print(
            f"ERROR: file system error:\n"
            f"  {exc}",
            file=sys.stderr
        )

        return 5

    except Exception as exc:

        print(
            f"ERROR: unexpected error:\n"
            f"  {exc}",
            file=sys.stderr
        )

        return 10

    return 0


if __name__ == "__main__":
    sys.exit(main())