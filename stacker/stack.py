import copy

from . import util


def _gather_parameters(stack_def, builder_parameters):
    """Merges builder provided & stack defined parameters.

    Ensures that more specificly defined parameters (ie: parameters defined
    specifically for the given stack: stack_name::parameter) override less
    specific parameters provided by the builder.

    Order of precedence:
        - builder defined stack specific (stack_name::parameter)
        - builder defined non-specific (parameter)
        - stack_def defined

    """
    parameters = copy.deepcopy(stack_def.get('parameters', {}))
    stack_specific_params = {}
    for key, value in builder_parameters.iteritems():
        stack = None
        if "::" in key:
            stack, key = key.split("::", 1)
        if not stack:
            # Non-stack specific, go ahead and add it
            parameters[key] = value
            continue
        # Gather stack specific params for later
        if stack == stack_def['name']:
            stack_specific_params[key] = value
    # Now update stack parameters with the stack specific parameters
    # ensuring they override generic parameters
    parameters.update(stack_specific_params)
    return parameters


class Stack(object):

    def __init__(self, definition, context, parameters=None, mappings=None):
        self.name = definition['name']
        self.fqn = context.get_fqn(self.name)
        self.definition = definition
        self.parameters = _gather_parameters(definition, parameters or {})
        self.mappings = mappings
        self.locked = definition.get('locked', False)
        # XXX this is temporary until we remove passing context down to the
        # blueprint
        self.context = copy.deepcopy(context)
        if isinstance(self.context.parameters, dict):
            self.context.parameters.update(self.parameters)

    def __repr__(self):
        return self.fqn

    @property
    def requires(self):
        requires = set(self.definition.get('requires', []))
        # Auto add dependencies when parameters reference the Ouptuts of
        # another stack.
        for value in self.parameters.values():
            if isinstance(value, basestring) and '::' in value:
                stack_name, _ = value.split('::')
            else:
                continue
            stack_fqn = self.context.get_fqn(stack_name)
            if stack_fqn not in requires:
                requires.add(stack_fqn)
        return requires

    @property
    def blueprint(self):
        if not hasattr(self, '_blueprint'):
            class_path = self.definition['class_path']
            blueprint_class = util.load_object_from_string(class_path)
            if not hasattr(blueprint_class, 'rendered'):
                raise AttributeError("Stack class %s does not have a "
                                     "'rendered' "
                                     "attribute." % (class_path,))
            self._blueprint = blueprint_class(
                name=self.name,
                context=self.context,
                mappings=self.mappings,
            )
        return self._blueprint
