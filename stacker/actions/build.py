import logging

from .base import BaseAction
from .. import exceptions, util
from ..providers.exceptions import StackDidNotChange
from ..plan import COMPLETE, SKIPPED, SUBMITTED, Plan

logger = logging.getLogger(__name__)


class Action(BaseAction):
    """Responsible for building & coordinating CloudFormation stacks.

    Generates the build plan based on stack dependencies (these dependencies
    are determined automatically based on references to output values from
    other stacks).

    The plan can then either be printed out as an outline or executed. If
    executed, each stack will get launched in order which entails:
        - Pushing the generated CloudFormation template to S3 if it has changed
        - Submitting either a build or update of the given stack to the
          `Provider`.
        - Stores the stack outputs for reference by other stacks.

    """

    def _resolve_parameters(self, outputs, parameters, blueprint):
        """Resolves parameters for a given blueprint.

        Given a list of parameters, first discard any parameters that the
        blueprint does not use. Then, if a remaining parameter is in the format
        <stack_name>::<output_name>, pull that output from the foreign
        stack.

        Args:
            outputs (dict): any outputs that can be referenced by other stacks
            parameters (dict): A dictionary of parameters provided by the
                stack definition
            blueprint (`stacker.blueprint.base.Blueprint`): A Blueprint object
                that is having the parameters applied to it.

        Returns:
            dict: The resolved parameters.

        """
        params = {}
        blueprint_params = blueprint.parameters
        for k, v in parameters.items():
            if k not in blueprint_params:
                logger.debug("Template %s does not use parameter %s.",
                             blueprint.name, k)
                continue
            value = v
            if isinstance(value, basestring) and '::' in value:
                # Get from the Output of another stack in the stack_map
                stack_name, output = value.split('::')
                stack_fqn = self.context.get_fqn(stack_name)
                # XXX check out this logic to see if this is what we really
                # want to do
                try:
                    stack_outputs = outputs[stack_fqn]
                except KeyError:
                    raise exceptions.StackDoesNotExist(stack_fqn)
                try:
                    value = stack_outputs[output]
                except KeyError:
                    raise exceptions.ParameterDoesNotExist(value)
            params[k] = value
        return params

    def _build_stack_tags(self, stack, template_url):
        """Builds a common set of tags to attach to a stack"""
        requires = [req for req in stack.requires]
        logger.debug("Stack %s required stacks: %s",
                     stack.name, requires)
        tags = {
            'template_url': template_url,
            'stacker_namespace': self.context.namespace,
        }
        if requires:
            tags['required_stacks'] = ':'.join(requires)
        return tags

    def _launch_stack(self, results, stack, **kwargs):
        """Handles the creating or updating of a stack in CloudFormation.

        Also makes sure that we don't try to create or update a stack while
        it is already updating or creating.

        """
        provider_stack = self.provider.get_stack(stack.fqn)
        if provider_stack and kwargs.get('status') is SUBMITTED:
            logger.debug(
                "Stack %s provider status: %s",
                stack.fqn,
                self.provider.get_stack_status(provider_stack),
            )
            if self.provider.is_stack_completed(provider_stack):
                return COMPLETE
            elif self.provider.is_stack_in_progress(provider_stack):
                logger.debug("Stack %s in progress.", stack.fqn)
                return SUBMITTED

        logger.info("Launching stack %s now.", stack.fqn)
        template_url = self.s3_stack_push(stack.blueprint)
        tags = self._build_stack_tags(stack, template_url)
        parameters = self._resolve_parameters(results, stack.parameters,
                                              stack.blueprint)
        required_params = [k for k, v in stack.blueprint.required_parameters]
        parameters = self._handle_missing_parameters(parameters,
                                                     required_params,
                                                     provider_stack)

        try:
            if not provider_stack:
                self.provider.create_stack(stack.fqn, template_url, parameters,
                                           tags)
            else:
                if stack.locked:
                    if not kwargs.get('force', False):
                        logger.info("Stack %s locked and not in force list. "
                                    "Refusing to update.", stack.name)
                        return SKIPPED
                    else:
                        logger.debug("Stack %s locked, but is in force list.",
                                     stack.name)
                self.provider.update_stack(stack.fqn, template_url, parameters,
                                           tags)
        except StackDidNotChange:
            return SKIPPED

        return SUBMITTED

    def _get_outputs(self, stack):
        """Gets all the outputs from a given stack in CloudFormation.

        Updates the local output cache with the values it finds.

        """
        provider_stack = self.provider.get_stack(stack.fqn)
        if not provider_stack:
            raise ValueError("Stack %s does not exist." % (stack.fqn,))
        stack_outputs = {}
        for output in provider_stack.outputs:
            logger.debug("    %s: %s", output.key, output.value)
            stack_outputs[output.key] = output.value
        return stack_outputs

    def _handle_missing_parameters(self, params, required_params,
                                   existing_stack=None):
        """Handles any missing parameters.

        If an existing_stack is provided, look up missing parameters there.

        Args:
            params (dict): key/value dictionary of stack definition parameters
            required_params (list): A list of required parameter names.
            existing_stack (`boto.cloudformation.stack.Stack`): A `Stack`
                object. If provided, will be searched for any missing
                parameters.

        Returns:
            list of tuples: The final list of key/value pairs returned as a
                list of tuples.

        Raises:
            MissingParameterException: Raised if a required parameter is
                still missing.

        """
        missing_params = list(set(required_params) - set(params.keys()))
        if existing_stack:
            stack_params = {p.key: p.value for p in existing_stack.parameters}
            for p in missing_params:
                if p in stack_params:
                    value = stack_params[p]
                    logger.debug("Using parameter %s from existing stack: %s",
                                 p, value)
                    params[p] = value
        final_missing = list(set(required_params) - set(params.keys()))
        if final_missing:
            raise exceptions.MissingParameterException(final_missing)

        return params.items()

    def _generate_plan(self, force_stacks=[]):
        plan = Plan(description='Create/Update stacks')
        stacks = self.context.get_stacks_dict()
        dependencies = self._get_dependencies()
        for stack_name in self.get_stack_execution_order(dependencies):
            force = stacks[stack_name].name in force_stacks
            plan.add(
                stacks[stack_name],
                run_func=self._launch_stack,
                completion_func=self._get_outputs,
                skip_func=self._get_outputs,
                requires=dependencies.get(stack_name),
                force=force,
            )
        return plan

    def _get_dependencies(self):
        dependencies = {}
        for stack in self.context.get_stacks():
            dependencies[stack.fqn] = stack.requires
        return dependencies

    def pre_run(self, outline=False, *args, **kwargs):
        """Any steps that need to be taken prior to running the action."""
        pre_build = self.context.config.get('pre_build')
        if not outline and pre_build:
            util.handle_hooks('pre_build', pre_build, self.provider.region,
                              self.context)

    def run(self, outline=False, force_stacks=[], *args, **kwargs):
        """Kicks off the build/update of the stacks in the stack_definitions.

        This is the main entry point for the Builder.

        """
        plan = self._generate_plan(force_stacks=force_stacks)
        if not outline:
            # need to generate a new plan to log since the outline sets the
            # steps to COMPLETE in order to log them
            debug_plan = self._generate_plan()
            debug_plan.outline(logging.DEBUG)
            logger.info("Launching stacks: %s", ', '.join(plan.keys()))
            plan.execute()
        else:
            plan.outline(execute_helper=True)

    def post_run(self, outline=False, *args, **kwargs):
        """Any steps that need to be taken after running the action."""
        post_build = self.context.config.get('post_build')
        if not outline and post_build:
            util.handle_hooks('post_build', post_build, self.context)
