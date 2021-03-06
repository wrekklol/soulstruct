from __future__ import annotations

import copy
import io
import logging
import struct
import typing as tp
from collections import OrderedDict

from soulstruct.bnd import BNDEntry
from soulstruct.core import SoulstructError
from soulstruct.params import enums
from soulstruct.params.paramdef import ParamDefBND
from soulstruct.utilities.core import BinaryStruct, read_chars_from_buffer

if tp.TYPE_CHECKING:
    from soulstruct.params.paramdef import ParamDef, ParamDefField
    from soulstruct.params.display_info.base import FieldDisplayInfo

# TODO: GameParam BND indices of params tables are different in PTD/DSR. I'm guessing it may not actually matter, and
#   that all the params tables are loaded and accessed by their names (e.g. 'OBJ_ACT_PARAM_ST').

_LOGGER = logging.getLogger(__name__)

_PARAMDEF_BND_PTD = None
_PARAMDEF_BND_DSR = None


def PARAMDEF_BND(game_version):
    global _PARAMDEF_BND_PTD, _PARAMDEF_BND_DSR
    if game_version.lower() == "ptde":
        if _PARAMDEF_BND_PTD is None:
            _PARAMDEF_BND_PTD = ParamDefBND("ptde")
        return _PARAMDEF_BND_PTD
    elif game_version.lower() == "dsr":
        if _PARAMDEF_BND_DSR is None:
            _PARAMDEF_BND_DSR = ParamDefBND("dsr")
        return _PARAMDEF_BND_DSR
    raise ValueError(f"Could not find bundled ParamDef for game version {repr(game_version)}.")


JUNK_ENTRY_NAMES = (b"\x80\x1e", b"\xfe\x1e")  # These appear in LIGHT_BANK in DS1.


class ParamError(SoulstructError):
    pass


class BitField:
    def __init__(self):
        self.__field = ""
        self.__offset = 0

    def unpack(self, buffer, bit_count):
        if self.__field == "":
            # Consume (and reverse) new one-byte bit field.
            self.__field = format(struct.unpack("<B", buffer.read(1))[0], "08b")[::-1]
        value = int(self.__field[self.__offset : self.__offset + bit_count][::-1], 2)
        self.__offset += bit_count
        if self.__offset >= 8:
            self.__field = ""
            self.__offset = self.__offset % 8
        return value

    def pack(self, value, bit_count):
        binary_value = bin(value)[2:]
        if len(binary_value) > bit_count:
            raise ValueError(
                f"Value {value} (binary: {binary_value}) of binary field is "
                f"larger than given bit count ({bit_count})."
            )
        binary_value = "0" * (bit_count - len(binary_value)) + binary_value  # leading zeroes
        self.__field += binary_value[::-1]
        if len(self.__field) >= 8:
            completed_bit_field = self.__field[:8]
            # Leftover bytes go into next lot (though this should never happen due to pad fields).
            self.__field = self.__field[8:] if len(self.__field) > 8 else ""
            return int(completed_bit_field[::-1], 2)  # reversed
        return None

    def pad(self):
        if self.__field:
            # Pad out existing non-empty bit field and write it.
            self.__field += "0" * (8 - len(self.__field))
            completed_byte = int(self.__field[::-1], 2)  # note reversal
            self.__field = ""
            return completed_byte
        return None

    def clear(self):
        self.__field = ""
        self.__offset = 0


class ParamEntry:
    def __init__(self, entry_source, paramdef, name=None):
        self.fields = OrderedDict()
        self.paramdef = paramdef  # type: ParamDef
        self.bit_field = BitField()

        if isinstance(entry_source, OrderedDict):
            if name is None:
                if "name" not in entry_source:
                    raise ValueError("Name must be specified in arguments or source dictionary.")
                self.name = entry_source["name"]
            elif isinstance(name, str):
                # TODO: Name needs to be converted to shift-jis?
                if "name" not in entry_source:
                    _LOGGER.warning(
                        f"Name in source dictionary of ParamEntry '{entry_source['name']}' will be overridden with "
                        f"argument value ('{name}')."
                    )
                self.name = entry_source["name"] = name
            else:
                raise ValueError("Name must be a string.")
        elif isinstance(entry_source, dict):
            raise TypeError("You must use an OrderedDict to create a ParamEntry. Try copying an existing entry first.")
        elif isinstance(entry_source, bytes):
            if name is None:
                raise ValueError("`name` argument must be given explictly alongside raw entry data.")
            self.name = name
            self.unpack(entry_source, name)

    def __iter__(self):
        return iter(self.fields.items())

    def __getitem__(self, field):
        if isinstance(field, int):
            try:
                field = list(self.fields.keys())[field]
            except IndexError:
                raise KeyError(f"No field with index {field}.")
        if isinstance(field, str):
            try:
                return self.fields[field]
            except KeyError:
                raise KeyError(f"No field with name '{field}' in entry {self.name}.")

    def __setitem__(self, field, value):
        if isinstance(field, int):
            try:
                field = list(self.fields.keys())[field]
            except IndexError:
                raise KeyError(f"No field with index {field}. (You cannot create new fields.)")
        if field not in self.fields:
            raise KeyError(f"Field '{field}' does not exist in params.")
        # TODO: Check value type is valid (or that it can be cast).
        self.fields[field] = value

    @property
    def field_names(self):
        if self.paramdef.param_info:
            return [field.name for field in self.paramdef.param_info["fields"]]
        else:
            return list(self.fields.keys())

    def get_paramdef_field(self, field_name: str) -> ParamDefField:
        return self.paramdef[field_name]

    def get_paramdef_field_display_info(self, field_name: str) -> FieldDisplayInfo:
        return self.paramdef[field_name].get_display_info(self)

    def __repr__(self):
        return f"\nName: {self.name}" + "".join([f"\n    {key} = {value}" for key, value in self.fields.items()])

    def copy(self):
        return copy.deepcopy(self)

    def unpack(self, entry_buffer, name: str):
        if isinstance(entry_buffer, bytes):
            entry_buffer = io.BytesIO(entry_buffer)

        for field in self.paramdef.fields:

            if field.bit_size < 8:
                field_value = self.bit_field.unpack(entry_buffer, field.bit_size)
            elif field.internal_type == "dummy8":
                self.bit_field.clear()
                field_value = entry_buffer.read(field.size)
                if not field_value == b"\0" * field.size:
                    raise ValueError(
                        f"Pad value of field {field} in entry {self.name} of ParamTable "
                        f"{self.paramdef.param_name} is not null: {field_value}."
                    )
            else:
                self.bit_field.clear()
                try:
                    field_type = getattr(enums, field.internal_type)
                except AttributeError:
                    if field.name == "sfxMultiplier":
                        field_type = enums.f32
                    else:
                        raise KeyError(
                            f"Field {field.name} in ParamTable {self.paramdef.param_name} has unknown "
                            f"internal type {field.internal_type} (debug type = {field.debug_type})."
                        )
                data = entry_buffer.read(field_type.size())
                try:
                    (field_value,) = struct.unpack(field_type.format(), data)
                except struct.error as e:
                    if field.debug_name in {"inverseToneMapMul", "sfxMultiplier"}:
                        # These fields are screwed up in m99 and default ToneMapBank.
                        field_value = 1.0
                    else:
                        print(data, field_type)
                        raise ValueError(
                            f"Could not unpack data for field {field}.\n"
                            f"Field type: {field_type}; Raw bytes: {data}\n"
                            f"Error:\n{str(e)}"
                        )

            self.fields[field.name] = field_value

        self.name = name

    def pack(self):
        packed_entry = b""
        for field_name, field_value in self.fields.items():  # These are ordered correctly already.
            field = self.paramdef[field_name]
            if field.bit_size < 8:
                # Add bits.
                completed_byte = self.bit_field.pack(field_value, field.bit_size)
                if completed_byte is not None:
                    packed_entry += struct.pack("<B", completed_byte)
                continue
            completed_byte = self.bit_field.pad()
            if completed_byte is not None:
                packed_entry += struct.pack("<B", completed_byte)
            if field.internal_type == "dummy8":
                # Write nulls.
                packed_entry += b"\x00" * field.size
                continue
            try:
                field_type = getattr(enums, field.internal_type)
            except AttributeError:
                if field.name == "sfxMultiplier":
                    field_type = enums.f32
                else:
                    raise ParamError(
                        f"Field {field.name} in ParamTable {self.paramdef.param_name} has unknown "
                        f"internal type {field.internal_type} (debug type = {field.debug_type})."
                    )
            if not isinstance(self[field.name], field_type.python_type()):
                raise ParamError(
                    f"Bad type: field {field.name} in entry {repr(self.name)} of table "
                    f"{self.paramdef.param_name} has value {self[field.name]} with type "
                    f"{type(self[field.name])}, but should have type {field_type.python_type()}."
                )
            if not field_type.minimum() <= self[field.name] <= field_type.maximum():
                _LOGGER.error(f"Error in field. Field data: {field}")
                raise ParamError(
                    f"Invalid: field {field.name} in entry {repr(self.name)} of table "
                    f"{self.paramdef.param_name} has out-of-range value {self[field.name]} "
                    f"(range is {field_type.minimum()} to {field_type.maximum()})."
                )
            packed_entry += struct.pack(field_type.format(), field_value)

        return packed_entry


class ParamTable:

    # TODO: This is currently for DeS/DS1 only.
    HEADER_STRUCT = BinaryStruct(
        ("name_data_offset", "I"),
        ("entry_data_offset", "H"),
        ("magic0", "H"),  # 0 or 1
        ("magic1", "H"),  # 1, 2, or 3
        ("entry_count", "H"),
        ("param_name", "32j"),
        ("big_endian", "b", 0),  # TODO: check, rather than assert
        ("magic2", "H"),  # TODO: Actually two format flag bytes.
        ("unknown", "B"),  # TODO: sometimes -1 in later formats.
    )

    ENTRY_POINTER_STRUCT = BinaryStruct(
        # These are packed together, and contain offsets into packed entry data and packed names.
        ("id", "i"),
        ("data_offset", "i"),
        ("name_offset", "i"),
    )

    entries: tp.Dict[int, ParamEntry]

    def __init__(self, param_source, paramdef_bnd):
        self.param_path = ""
        self.param_name = ""  # internal name (shift-jis) with capitals and underscores
        self._paramdef_bnd = PARAMDEF_BND(paramdef_bnd) if isinstance(paramdef_bnd, str) else paramdef_bnd
        self.entries = {}
        self.__magic = []
        self.__unknown = None
        self._nickname = ""

        if isinstance(param_source, dict):
            self.entries = param_source

        elif isinstance(param_source, bytes):
            self.unpack(io.BytesIO(param_source))

        elif isinstance(param_source, str):
            self.param_path = param_source
            with open(param_source, "rb") as data:
                self.unpack(data)

        elif isinstance(param_source, BNDEntry):
            self.unpack(io.BytesIO(param_source.data))

        else:
            raise TypeError(f"Invalid `param_source` type: {type(param_source)}")

    def __getitem__(self, entry_id):
        if entry_id in self.entries:
            return self.entries[entry_id]
        raise KeyError(f"No entry with ID {entry_id} in {self.param_name}.")

    def __setitem__(self, entry_index, entry):
        if isinstance(entry, dict):
            if "name" not in entry:
                raise ValueError("New entry must have a 'name' field.")
            entry = ParamEntry(entry, self._paramdef_bnd[self.param_name])
        if isinstance(entry, ParamEntry):
            self.entries[entry_index] = entry
        else:
            raise TypeError("New entry must be a ParamEntry or a dictionary that contains all required fields.")

    def keys(self):
        return self.entries.keys()

    def values(self):
        return self.entries.values()

    def items(self):
        return self.entries.items()

    def __iter__(self):
        return iter(self.entries)

    def __len__(self):
        return len(self.entries)

    def pop(self, entry_id):
        return self.entries.pop(entry_id)

    @property
    def paramdef(self):
        return self._paramdef_bnd[self.param_name]

    @property
    def param_info(self):
        return self.paramdef.param_info

    @property
    def field_names(self):
        if self.paramdef.param_info:
            return [field.name for field in self.paramdef.param_info["fields"]]
        else:
            return list(self.entries[0].fields.keys())

    @property
    def nickname(self):
        """Could return None for ambiguous tables like 'PlayerBehaviors'. Handled separately."""
        return self.paramdef.param_info["nickname"]

    # TODO: __repr__ method returns basic information about ParamTable (but not entire entry list).

    def unpack(self, param_buffer):
        header = self.HEADER_STRUCT.unpack(param_buffer)
        self.param_name = header["param_name"]
        self.__magic = [header["magic0"], header["magic1"], header["magic2"]]
        self.__unknown = header["unknown"]
        # Entry data offset in header not used. (It's an unsigned short, yet doesn't limit entry count to 5461.)
        name_data_offset = header["name_data_offset"]  # CANNOT BE TRUSTED IN VANILLA FILES! Off by +12 bytes.

        # Load entry pointer data.
        entry_pointers = self.ENTRY_POINTER_STRUCT.unpack_count(param_buffer, count=header["entry_count"])
        entry_data_offset = param_buffer.tell()  # Reliable entry data offset.

        # Entry size is lazily determined. TODO: Unpack entry data in sequence and associate with names separately.
        if len(entry_pointers) == 0:
            return
        elif len(entry_pointers) == 1:
            # NOTE: The only vanilla param in Dark Souls with one entry is LEVELSYNC_PARAM_ST (Remastered only),
            # for which the entry size is hard-coded here. Otherwise, we can trust the repacked offset from Soulstruct
            # (and SoulsFormats, etc.).
            if self.param_name == "LEVELSYNC_PARAM_ST":
                entry_size = 220
            else:
                entry_size = name_data_offset - entry_data_offset
        else:
            entry_size = entry_pointers[1]["data_offset"] - entry_pointers[0]["data_offset"]

        # Note that we no longer need to track buffer offset.
        for entry_struct in entry_pointers:
            param_buffer.seek(entry_struct["data_offset"])
            entry_data = param_buffer.read(entry_size)
            if entry_struct["name_offset"] != 0:
                try:
                    name = read_chars_from_buffer(
                        param_buffer,
                        offset=entry_struct["name_offset"],
                        encoding="shift_jis_2004",
                        reset_old_offset=False,  # no need to reset
                        ignore_encoding_error_for_these_chars=JUNK_ENTRY_NAMES,
                    )
                except ValueError:
                    param_buffer.seek(entry_struct["name_offset"])
                    _LOGGER.error(
                        f"Could not find null termination for entry name string in {self.param_name}.\n"
                        f"    Header: {header}\n"
                        f"    Entry Struct: {entry_struct}\n"
                        f"    30 chrs of name data: {param_buffer.read(30)}"
                    )
                    raise
            else:
                name = ""
            self.entries[entry_struct["id"]] = ParamEntry(entry_data, self.paramdef, name=name)

    def pack(self, sort=True):
        # if len(self.entries) > 5461:
        #     raise SoulstructError(
        #         f"ParamTable {self.param_name} has {len(self.entries)} entries, which is more than a "
        #         f"DS1 Param can store (5461). Remove some entries before packing it.")

        sorted_entries = sorted(self.entries.items()) if sort else self.entries.items()

        current_name_offset = 0
        name_offset_list = []
        data_offset = 0
        data_offset_list = []
        packed_names = b""
        packed_data = b""

        for entry_id, entry in sorted_entries:

            # Pack names with relative offsets (to be globally offset later).
            if entry.name in JUNK_ENTRY_NAMES:
                name_z_str = entry.name + b"\0"  # never decoded
            else:
                name_z_str = entry.name.encode("shift_jis_2004") + b"\0"
            packed_names += name_z_str
            name_offset_list.append(current_name_offset)
            current_name_offset += len(name_z_str)

            # Pack entry data.
            packed_entry = entry.pack()
            packed_data += packed_entry
            data_offset_list.append(data_offset)
            data_offset += len(packed_entry)

        entry_pointer_table_offset = self.HEADER_STRUCT.size
        entry_data_offset = entry_pointer_table_offset + self.ENTRY_POINTER_STRUCT.size * len(sorted_entries)
        name_data_offset = entry_data_offset + len(packed_data)

        # Entries.
        entry_pointer_data = b""
        for i, (entry_id, _) in enumerate(sorted_entries):
            entry_pointer_data += self.ENTRY_POINTER_STRUCT.pack(
                dict(
                    id=entry_id,
                    data_offset=entry_data_offset + data_offset_list[i],
                    name_offset=name_data_offset + name_offset_list[i],
                )
            )

        # Header.
        header = self.HEADER_STRUCT.pack(
            dict(
                name_data_offset=name_data_offset,
                entry_data_offset=min(entry_data_offset, 2 ** 16 - 1),  # This ushort field isn't actually used.
                magic0=self.__magic[0],
                magic1=self.__magic[1],
                entry_count=len(sorted_entries),
                param_name=self.param_name,
                magic2=self.__magic[2],
                unknown=self.__unknown,
            )
        )

        return header + entry_pointer_data + packed_data + packed_names

    def write_packed(self, param_path=None):
        if param_path is None:
            if self.param_path:
                param_path = self.param_path
            else:
                raise ValueError("Param path could not be determined automatically (must be specified).")
        if not param_path.endswith(".param"):
            param_path += ".param"

        with open(param_path, "wb") as output:
            output.write(self.pack())

    def get_range(self, start, count):
        return [(param_id, self[param_id]) for param_id in sorted(self.entries)[start : start + count]]

    def copy(self):
        return copy.deepcopy(self)


class DrawParamTable(ParamTable):
    def get_nonzero_entries(self, ignore_polyg=True):
        """ Filters table entries and returns only those with a non-empty name that does not start with '0' (or,
        by default, 'PolyG', which I assume is cutscene-specific lighting). """
        if ignore_polyg:
            return {
                index: entry for index, entry in self.entries.items() if entry.name and not entry.name.startswith("0")
            }
        return {
            index: entry
            for index, entry in self.entries.items()
            if entry.name and not entry.name.startswith("0") and not entry.name.lower().startswith("polyg")
        }
