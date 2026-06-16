"""
DDS IdlStruct mirror of brainco::msg::dds_::TouchHand_ for subscribing to the
touch topic published by brainco_hand_server on PC2.

CDR wire format must match the IDL at
g1_29dof_brainco_avp/services/brainco_hand_service/idl/TouchHand_.idl exactly —
field names, types, and order. The C++ side is generated from the same IDL.
Layout: 5 fingers [thumb, index, middle, ring, pinky], flattened.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

import cyclonedds.idl as idl
import cyclonedds.idl.annotations as annotate
import cyclonedds.idl.types as types


@dataclass
@annotate.final
@annotate.autoid("sequential")
class TouchHand_(idl.IdlStruct, typename="brainco.msg.dds_.TouchHand_"):
    stamp_ns: types.uint64
    normal_force: types.array[types.uint16, 15]           # 5 fingers x 3
    tangential_force: types.array[types.uint16, 15]       # 5 x 3
    tangential_direction: types.array[types.uint16, 15]   # 5 x 3
    self_proximity: types.array[types.uint32, 10]         # 5 x 2
    mutual_proximity: types.array[types.uint32, 5]        # 5
    status: types.array[types.uint16, 5]                  # 5
