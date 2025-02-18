from typing import Optional, Tuple, List, Union
from slither.core.declarations import (
    Contract,
    Structure,
    Enum,
    SolidityVariableComposed,
    SolidityVariable,
    Function,
)
from slither.core.solidity_types import (
    Type,
    ElementaryType,
    ArrayType,
    MappingType,
    UserDefinedType,
)
from slither.core.variables.local_variable import LocalVariable
from slither.core.variables.local_variable_init_from_tuple import LocalVariableInitFromTuple
from slither.core.variables.state_variable import StateVariable
from slither.analyses.data_dependency.data_dependency import get_dependencies
from slither.core.variables.variable import Variable
from slither.core.expressions.literal import Literal
from slither.core.expressions.identifier import Identifier
from slither.core.expressions.call_expression import CallExpression
from slither.core.expressions.assignment_operation import AssignmentOperation
from slither.core.cfg.node import Node, NodeType
from slither.slithir.operations import (
    Operation,
    Assignment,
    Index,
    Member,
    Length,
    Binary,
    Unary,
    Condition,
    NewArray,
    NewStructure,
    NewContract,
    NewElementaryType,
    SolidityCall,
    Delete,
    EventCall,
    LibraryCall,
    InternalDynamicCall,
    HighLevelCall,
    LowLevelCall,
    TypeConversion,
    Return,
    Transfer,
    Send,
    Unpack,
    InitArray,
    InternalCall,
)
from slither.slithir.variables import (
    TemporaryVariable,
    TupleVariable,
    Constant,
    ReferenceVariable,
)
from slither.tools.read_storage.read_storage import SlotInfo, SlitherReadStorage


# pylint: disable=too-many-locals
def compare(
    v1: Contract, v2: Contract
) -> Tuple[
    List[Variable], List[Variable], List[Variable], List[Function], List[Function], List[Function]
]:
    """
    Compares two versions of a contract. Most useful for upgradeable (logic) contracts,
    but does not require that Contract.is_upgradeable returns true for either contract.

    Args:
        v1: Original version of (upgradeable) contract
        v2: Updated version of (upgradeable) contract

    Returns:
        missing-vars-in-v2: list[Variable],
        new-variables: list[Variable],
        tainted-variables: list[Variable],
        new-functions: list[Function],
        modified-functions: list[Function],
        tainted-functions: list[Function]
    """

    order_vars1 = [
        v for v in v1.state_variables_ordered if not v.is_constant and not v.is_immutable
    ]
    order_vars2 = [
        v for v in v2.state_variables_ordered if not v.is_constant and not v.is_immutable
    ]
    func_sigs1 = [function.solidity_signature for function in v1.functions]
    func_sigs2 = [function.solidity_signature for function in v2.functions]

    missing_vars_in_v2 = []
    new_variables = []
    tainted_variables = []
    new_functions = []
    modified_functions = []
    tainted_functions = []

    # Since this is not a detector, include any missing variables in the v2 contract
    if len(order_vars2) < len(order_vars1):
        missing_vars_in_v2.extend(get_missing_vars(v1, v2))

    # Find all new and modified functions in the v2 contract
    new_modified_functions = []
    new_modified_function_vars = []
    for sig in func_sigs2:
        function = v2.get_function_from_signature(sig)
        orig_function = v1.get_function_from_signature(sig)
        if sig not in func_sigs1:
            new_modified_functions.append(function)
            new_functions.append(function)
            new_modified_function_vars += (
                function.state_variables_read + function.state_variables_written
            )
        elif not function.is_constructor_variables and is_function_modified(
            orig_function, function
        ):
            new_modified_functions.append(function)
            modified_functions.append(function)
            new_modified_function_vars += (
                function.state_variables_read + function.state_variables_written
            )

    # Find all unmodified functions that call a modified function or read/write the
    # same state variable(s) as a new/modified function, i.e., tainted functions
    for function in v2.functions:
        if (
            function in new_modified_functions
            or function.is_constructor
            or function.name.startswith("slither")
        ):
            continue
        modified_calls = [
            func for func in new_modified_functions if func in function.internal_calls
        ]
        tainted_vars = [
            var
            for var in set(new_modified_function_vars)
            if var in function.variables_read_or_written
            and not var.is_constant
            and not var.is_immutable
        ]
        if len(modified_calls) > 0 or len(tainted_vars) > 0:
            tainted_functions.append(function)

    # Find all new or tainted variables, i.e., variables that are read or written by a new/modified/tainted function
    for var in order_vars2:
        read_by = v2.get_functions_reading_from_variable(var)
        written_by = v2.get_functions_writing_to_variable(var)
        if v1.get_state_variable_from_name(var.name) is None:
            new_variables.append(var)
        elif any(
            func in read_by or func in written_by
            for func in new_modified_functions + tainted_functions
        ):
            tainted_variables.append(var)

    return (
        missing_vars_in_v2,
        new_variables,
        tainted_variables,
        new_functions,
        modified_functions,
        tainted_functions,
    )


def get_missing_vars(v1: Contract, v2: Contract) -> List[StateVariable]:
    """
    Gets all non-constant/immutable StateVariables that appear in v1 but not v2
    Args:
        v1: Contract version 1
        v2: Contract version 2

    Returns:
        List of StateVariables from v1 missing in v2
    """
    results = []
    order_vars1 = [
        v for v in v1.state_variables_ordered if not v.is_constant and not v.is_immutable
    ]
    order_vars2 = [
        v for v in v2.state_variables_ordered if not v.is_constant and not v.is_immutable
    ]
    if len(order_vars2) < len(order_vars1):
        for variable in order_vars1:
            if variable.name not in [v.name for v in order_vars2]:
                results.append(variable)
    return results


def is_function_modified(f1: Function, f2: Function) -> bool:
    """
    Compares two versions of a function, and returns True if the function has been modified.
    First checks whether the functions' content hashes are equal to quickly rule out identical functions.
    Walks the CFGs and compares IR operations if hashes differ to rule out false positives, i.e., from changed comments.

    Args:
        f1: Original version of the function
        f2: New version of the function

    Returns:
        True if the functions differ, otherwise False
    """
    # If the function content hashes are the same, no need to investigate the function further
    if f1.source_mapping.content_hash == f2.source_mapping.content_hash:
        return False
    # If the hashes differ, it is possible a change in a name or in a comment could be the only difference
    # So we need to resort to walking through the CFG and comparing the IR operations
    queue_f1 = [f1.entry_point]
    queue_f2 = [f2.entry_point]
    visited = []
    while len(queue_f1) > 0 and len(queue_f2) > 0:
        node_f1 = queue_f1.pop(0)
        node_f2 = queue_f2.pop(0)
        visited.extend([node_f1, node_f2])
        queue_f1.extend(son for son in node_f1.sons if son not in visited)
        queue_f2.extend(son for son in node_f2.sons if son not in visited)
        for i, ir in enumerate(node_f1.irs):
            if encode_ir_for_compare(ir) != encode_ir_for_compare(node_f2.irs[i]):
                return True
    return False


# pylint: disable=too-many-branches
def ntype(_type: Union[Type, str]) -> str:
    if isinstance(_type, ElementaryType):
        _type = str(_type)
    elif isinstance(_type, ArrayType):
        if isinstance(_type.type, ElementaryType):
            _type = str(_type)
        else:
            _type = "user_defined_array"
    elif isinstance(_type, Structure):
        _type = str(_type)
    elif isinstance(_type, Enum):
        _type = str(_type)
    elif isinstance(_type, MappingType):
        _type = str(_type)
    elif isinstance(_type, UserDefinedType):
        if isinstance(_type.type, Contract):
            _type = f"contract({_type.type.name})"
        elif isinstance(_type.type, Structure):
            _type = f"struct({_type.type.name})"
        elif isinstance(_type.type, Enum):
            _type = f"enum({_type.type.name})"
    else:
        _type = str(_type)

    _type = _type.replace(" memory", "")
    _type = _type.replace(" storage ref", "")

    if "struct" in _type:
        return "struct"
    if "enum" in _type:
        return "enum"
    if "tuple" in _type:
        return "tuple"
    if "contract" in _type:
        return "contract"
    if "mapping" in _type:
        return "mapping"
    return _type.replace(" ", "_")


# pylint: disable=too-many-branches
def encode_ir_for_compare(ir: Operation) -> str:
    # operations
    if isinstance(ir, Assignment):
        return f"({encode_var_for_compare(ir.lvalue)}):=({encode_var_for_compare(ir.rvalue)})"
    if isinstance(ir, Index):
        return f"index({ntype(ir.index_type)})"
    if isinstance(ir, Member):
        return "member"  # .format(ntype(ir._type))
    if isinstance(ir, Length):
        return "length"
    if isinstance(ir, Binary):
        return f"binary({str(ir.variable_left)}{str(ir.type)}{str(ir.variable_right)})"
    if isinstance(ir, Unary):
        return f"unary({str(ir.type)})"
    if isinstance(ir, Condition):
        return f"condition({encode_var_for_compare(ir.value)})"
    if isinstance(ir, NewStructure):
        return "new_structure"
    if isinstance(ir, NewContract):
        return "new_contract"
    if isinstance(ir, NewArray):
        return f"new_array({ntype(ir.array_type)})"
    if isinstance(ir, NewElementaryType):
        return f"new_elementary({ntype(ir.type)})"
    if isinstance(ir, Delete):
        return f"delete({encode_var_for_compare(ir.lvalue)},{encode_var_for_compare(ir.variable)})"
    if isinstance(ir, SolidityCall):
        return f"solidity_call({ir.function.full_name})"
    if isinstance(ir, InternalCall):
        return f"internal_call({ntype(ir.type_call)})"
    if isinstance(ir, EventCall):  # is this useful?
        return "event"
    if isinstance(ir, LibraryCall):
        return "library_call"
    if isinstance(ir, InternalDynamicCall):
        return "internal_dynamic_call"
    if isinstance(ir, HighLevelCall):  # TODO: improve
        return "high_level_call"
    if isinstance(ir, LowLevelCall):  # TODO: improve
        return "low_level_call"
    if isinstance(ir, TypeConversion):
        return f"type_conversion({ntype(ir.type)})"
    if isinstance(ir, Return):  # this can be improved using values
        return "return"  # .format(ntype(ir.type))
    if isinstance(ir, Transfer):
        return f"transfer({encode_var_for_compare(ir.call_value)})"
    if isinstance(ir, Send):
        return f"send({encode_var_for_compare(ir.call_value)})"
    if isinstance(ir, Unpack):  # TODO: improve
        return "unpack"
    if isinstance(ir, InitArray):  # TODO: improve
        return "init_array"

    # default
    return ""


# pylint: disable=too-many-branches
def encode_var_for_compare(var: Variable) -> str:

    # variables
    if isinstance(var, Constant):
        return f"constant({ntype(var.type)})"
    if isinstance(var, SolidityVariableComposed):
        return f"solidity_variable_composed({var.name})"
    if isinstance(var, SolidityVariable):
        return f"solidity_variable{var.name}"
    if isinstance(var, TemporaryVariable):
        return "temporary_variable"
    if isinstance(var, ReferenceVariable):
        return f"reference({ntype(var.type)})"
    if isinstance(var, LocalVariable):
        return f"local_solc_variable({var.location})"
    if isinstance(var, StateVariable):
        return f"state_solc_variable({ntype(var.type)})"
    if isinstance(var, LocalVariableInitFromTuple):
        return "local_variable_init_tuple"
    if isinstance(var, TupleVariable):
        return "tuple_variable"

    # default
    return ""


def get_proxy_implementation_slot(proxy: Contract) -> Optional[SlotInfo]:
    """
    Gets information about the storage slot where a proxy's implementation address is stored.
    Args:
        proxy: A Contract object (proxy.is_upgradeable_proxy should be true).

    Returns:
        (`SlotInfo`) | None : A dictionary of the slot information.
    """

    delegate = get_proxy_implementation_var(proxy)
    if isinstance(delegate, StateVariable):
        if not delegate.is_constant and not delegate.is_immutable:
            srs = SlitherReadStorage([proxy], 20)
            return srs.get_storage_slot(delegate, proxy)
        if delegate.is_constant and delegate.type.name == "bytes32":
            return SlotInfo(
                name=delegate.name,
                type_string="address",
                slot=int(delegate.expression.value, 16),
                size=160,
                offset=0,
            )
    return None


def get_proxy_implementation_var(proxy: Contract) -> Optional[Variable]:
    """
    Gets the Variable that stores a proxy's implementation address. Uses data dependency to trace any LocalVariable
    that is passed into a delegatecall as the target address back to its data source, ideally a StateVariable.
    Can return a newly created StateVariable if an `sload` from a hardcoded storage slot is found in assembly.
    Args:
        proxy: A Contract object (proxy.is_upgradeable_proxy should be true).

    Returns:
        (`Variable`) | None : The variable, ideally a StateVariable, which stores the proxy's implementation address.
    """
    if not proxy.is_upgradeable_proxy or not proxy.fallback_function:
        return None

    delegate = find_delegate_in_fallback(proxy)
    if isinstance(delegate, LocalVariable):
        dependencies = get_dependencies(delegate, proxy)
        try:
            delegate = next(var for var in dependencies if isinstance(var, StateVariable))
        except StopIteration:
            return delegate
    return delegate


def find_delegate_in_fallback(proxy: Contract) -> Optional[Variable]:
    """
    Searches a proxy's fallback function for a delegatecall, then extracts the Variable being passed in as the target.
    Can return a newly created StateVariable if an `sload` from a hardcoded storage slot is found in assembly.
    Should typically be called by get_proxy_implementation_var(proxy).
    Args:
        proxy: A Contract object (should have a fallback function).

    Returns:
        (`Variable`) | None : The variable being passed as the destination argument in a delegatecall in the fallback.
    """
    delegate: Optional[Variable] = None
    fallback = proxy.fallback_function
    for node in fallback.all_nodes():
        for ir in node.irs:
            if isinstance(ir, LowLevelCall) and ir.function_name == "delegatecall":
                delegate = ir.destination
        if delegate is not None:
            break
        if (
            node.type == NodeType.ASSEMBLY
            and isinstance(node.inline_asm, str)
            and "delegatecall" in node.inline_asm
        ):
            delegate = extract_delegate_from_asm(proxy, node)
        elif node.type == NodeType.EXPRESSION:
            expression = node.expression
            if isinstance(expression, AssignmentOperation):
                expression = expression.expression_right
            if (
                isinstance(expression, CallExpression)
                and "delegatecall" in str(expression.called)
                and len(expression.arguments) > 1
            ):
                dest = expression.arguments[1]
                if isinstance(dest, CallExpression) and "sload" in str(dest.called):
                    dest = dest.arguments[0]
                if isinstance(dest, Identifier):
                    delegate = dest.value
                    break
                if (
                    isinstance(dest, Literal) and len(dest.value) == 66
                ):  # 32 bytes = 64 chars + "0x" = 66 chars
                    # Storage slot is not declared as a constant, but rather is hardcoded in the assembly,
                    # so create a new StateVariable to represent it.
                    delegate = create_state_variable_from_slot(dest.value)
                    break
    return delegate


def extract_delegate_from_asm(contract: Contract, node: Node) -> Optional[Variable]:
    """
    Finds a Variable with a name matching the argument passed into a delegatecall, when all we have is an Assembly node
    with a block of code as one long string. Usually only the case for solc versions < 0.6.0.
    Can return a newly created StateVariable if an `sload` from a hardcoded storage slot is found in assembly.
    Should typically be called by find_delegate_in_fallback(proxy).
    Args:
        contract: The parent Contract.
        node: The Assembly Node (i.e., node.type == NodeType.ASSEMBLY)

    Returns:
        (`Variable`) | None : The variable being passed as the destination argument in a delegatecall in the fallback.
    """
    asm_split = str(node.inline_asm).split("\n")
    asm = next(line for line in asm_split if "delegatecall" in line)
    params = asm.split("call(")[1].split(", ")
    dest = params[1]
    if dest.endswith(")") and not dest.startswith("sload("):
        dest = params[2]
    if dest.startswith("sload("):
        dest = dest.replace(")", "(").split("(")[1]
        if dest.startswith("0x"):
            return create_state_variable_from_slot(dest)
        if dest.isnumeric():
            slot_idx = int(dest)
            return next(
                (
                    v
                    for v in contract.state_variables_ordered
                    if SlitherReadStorage.get_variable_info(contract, v)[0] == slot_idx
                ),
                None,
            )
        for v in node.function.variables_read_or_written:
            if v.name == dest:
                if isinstance(v, LocalVariable) and v.expression is not None:
                    e = v.expression
                    if isinstance(e, Identifier) and isinstance(e.value, StateVariable):
                        v = e.value
                        # Fall through, return constant storage slot
                if isinstance(v, StateVariable) and v.is_constant:
                    return v
    if "_fallback_asm" in dest or "_slot" in dest:
        dest = dest.split("_")[0]
    return find_delegate_from_name(contract, dest, node.function)


def find_delegate_from_name(
    contract: Contract, dest: str, parent_func: Function
) -> Optional[Variable]:
    """
    Searches for a variable with a given name, starting with StateVariables declared in the contract, followed by
    LocalVariables in the parent function, either declared in the function body or as parameters in the signature.
    Can return a newly created StateVariable if an `sload` from a hardcoded storage slot is found in assembly.
    Args:
        contract: The Contract object to search.
        dest: The variable name to search for.
        parent_func: The Function object to search.

    Returns:
        (`Variable`) | None : The variable with the matching name, if found
    """
    for sv in contract.state_variables:
        if sv.name == dest:
            return sv
    for lv in parent_func.local_variables:
        if lv.name == dest:
            return lv
    for pv in parent_func.parameters + parent_func.returns:
        if pv.name == dest:
            return pv
    if parent_func.contains_assembly:
        for node in parent_func.all_nodes():
            if node.type == NodeType.ASSEMBLY and isinstance(node.inline_asm, str):
                asm = next(
                    (
                        s
                        for s in node.inline_asm.split("\n")
                        if f"{dest}:=sload(" in s.replace(" ", "")
                    ),
                    None,
                )
                if asm:
                    slot = asm.split("sload(")[1].split(")")[0]
                    if slot.startswith("0x"):
                        return create_state_variable_from_slot(slot, name=dest)
                    try:
                        slot_idx = int(slot)
                        return next(
                            (
                                v
                                for v in contract.state_variables_ordered
                                if SlitherReadStorage.get_variable_info(contract, v)[0] == slot_idx
                            ),
                            None,
                        )
                    except TypeError:
                        continue
    return None


def create_state_variable_from_slot(slot: str, name: str = None) -> Optional[StateVariable]:
    """
    Creates a new StateVariable object to wrap a hardcoded storage slot found in assembly.
    Args:
        slot: The storage slot hex string.
        name: Optional name for the variable. The slot string is used if name is not provided.

    Returns:
        A newly created constant StateVariable of type bytes32, with the slot as the variable's expression and name,
        if slot matches the length and prefix of a bytes32. Otherwise, returns None.
    """
    if len(slot) == 66 and slot.startswith("0x"):  # 32 bytes = 64 chars + "0x" = 66 chars
        # Storage slot is not declared as a constant, but rather is hardcoded in the assembly,
        # so create a new StateVariable to represent it.
        v = StateVariable()
        v.is_constant = True
        v.expression = Literal(slot, ElementaryType("bytes32"))
        if name is not None:
            v.name = name
        else:
            v.name = slot
        v.type = ElementaryType("bytes32")
        return v
    # This should probably also handle hashed strings, but for now return None
    return None
